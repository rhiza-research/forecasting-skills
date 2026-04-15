# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "xarray",
#   "zarr",
#   "matplotlib",
#   "numpy",
# ]
# ///
"""Render a heatmap or timeseries PNG from a Rhiza Envelope Zarr."""

import argparse
import sys
from pathlib import Path


def _cf_dim(obj, cf_name):
    try:
        return obj.cf[cf_name].name
    except KeyError:
        return None


def _parse_index(spec):
    if not spec:
        return {}
    out = {}
    for part in spec.split(","):
        k, _, v = part.partition("=")
        out[k.strip()] = int(v.strip())
    return out


def _reduce_to_2d(da, lat_dim, lon_dim, overrides):
    for dim in list(da.dims):
        if dim in (lat_dim, lon_dim):
            continue
        if dim in overrides:
            da = da.isel({dim: overrides[dim]}, drop=True)
        elif dim == "number":
            da = da.mean(dim)
        elif da.sizes[dim] == 1:
            da = da.squeeze(dim, drop=True)
        else:
            da = da.isel({dim: 0}, drop=True)
    if da.ndim != 2:
        raise ValueError(f"Could not reduce to 2D; residual dims: {da.dims}")
    return da.transpose(lat_dim, lon_dim)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--variable", "-v")
    p.add_argument("--style", choices=["heatmap", "timeseries"], default="heatmap")
    p.add_argument("--colormap", default="viridis")
    p.add_argument("--title")
    p.add_argument("--index", help="Slice spec like 'step=3,number=0'")
    args = p.parse_args()

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import xarray as xr

    src = Path(args.input)
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    variable = args.variable or (list(ds.data_vars)[0] if ds.data_vars else None)
    if not variable or variable not in ds:
        print(
            f"Error: no usable variable. Available: {list(ds.data_vars)}",
            file=sys.stderr,
        )
        sys.exit(2)
    da = ds[variable]
    overrides = _parse_index(args.index)

    fig, ax = plt.subplots(figsize=(10, 6))
    if args.style == "heatmap":
        lat_dim = _cf_dim(da, "latitude")
        lon_dim = _cf_dim(da, "longitude")
        if lat_dim is None or lon_dim is None:
            print(
                f"Error: heatmap requires lat/lon coords; got {list(da.dims)}.",
                file=sys.stderr,
            )
            sys.exit(2)
        slab = _reduce_to_2d(da, lat_dim, lon_dim, overrides)
        slab.plot.pcolormesh(
            ax=ax, x=lon_dim, y=lat_dim, cmap=args.colormap, add_colorbar=True
        )
        ax.set_xlabel(lon_dim)
        ax.set_ylabel(lat_dim)
    else:
        step_dim = "step" if "step" in da.dims else _cf_dim(da, "time")
        if step_dim is None:
            print(
                f"Error: timeseries needs 'step' or 'time'; got {list(da.dims)}.",
                file=sys.stderr,
            )
            sys.exit(2)
        reduce_dims = [d for d in da.dims if d != step_dim]
        da.mean(reduce_dims).plot(ax=ax)
        ax.set_xlabel(step_dim)

    ax.set_title(args.title or f"{variable} ({args.style})")
    fig.tight_layout()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
