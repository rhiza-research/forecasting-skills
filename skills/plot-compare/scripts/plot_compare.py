# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "xarray",
#   "zarr",
#   "matplotlib",
#   "numpy",
# ]
# ///
"""Side-by-side multi-panel PNG comparing two Rhiza Envelope Zarrs."""

import argparse
import sys
from pathlib import Path


LAT_ALIASES = ("latitude", "lat", "y")
LON_ALIASES = ("longitude", "lon", "x")


def _pick_dim(obj, candidates):
    for name in candidates:
        if name in obj.dims:
            return name
    return None


def _pick_time_dim(ds, override):
    if override:
        return override
    if "time" in ds.dims:
        return "time"
    if "step" in ds.dims:
        return "step"
    return None


def _is_station(ds):
    return "station_id" in ds.dims


def _panel_values(da, time_dim, i):
    sel = da.isel({time_dim: i})
    return sel


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--a", required=True, help="First Zarr input")
    p.add_argument("--b", required=True, help="Second Zarr input")
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--variable", "-v")
    p.add_argument("--colormap", default="viridis")
    p.add_argument("--title")
    p.add_argument("--panels", type=int, default=3)
    p.add_argument("--time-dim")
    args = p.parse_args()

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import xarray as xr

    for pth in (args.a, args.b):
        if not Path(pth).exists():
            print(f"Error: {pth} not found.", file=sys.stderr)
            sys.exit(2)

    ds_a = xr.open_zarr(args.a, consolidated=False)
    ds_b = xr.open_zarr(args.b, consolidated=False)
    variable = args.variable or (list(ds_a.data_vars)[0] if ds_a.data_vars else None)
    if variable is None or variable not in ds_a or variable not in ds_b:
        print(
            f"Error: variable '{variable}' must exist in both inputs. "
            f"A: {list(ds_a.data_vars)}  B: {list(ds_b.data_vars)}",
            file=sys.stderr,
        )
        sys.exit(2)

    td_a = _pick_time_dim(ds_a, args.time_dim)
    td_b = _pick_time_dim(ds_b, args.time_dim)
    if td_a is None or td_b is None:
        print(
            f"Error: both inputs need a time/step dim. A: {list(ds_a.dims)}  "
            f"B: {list(ds_b.dims)}",
            file=sys.stderr,
        )
        sys.exit(2)

    n = min(args.panels, ds_a.sizes[td_a], ds_b.sizes[td_b])
    if n < 1:
        print("Error: no overlapping panels to plot.", file=sys.stderr)
        sys.exit(1)

    da_a = ds_a[variable]
    da_b = ds_b[variable]

    # Reduce non-time, non-spatial, non-station dims to a single slice (e.g. number=0).
    def _flatten(da, time_dim):
        for d in list(da.dims):
            if d in (time_dim, "station_id", *LAT_ALIASES, *LON_ALIASES):
                continue
            da = da.mean(d) if d == "number" else da.isel({d: 0}, drop=True)
        return da

    da_a = _flatten(da_a, td_a)
    da_b = _flatten(da_b, td_b)

    vmax = float(np.nanmax([da_a.max().values, da_b.max().values]))
    vmin = float(np.nanmin([da_a.min().values, da_b.min().values]))

    fig, axes = plt.subplots(2, n, figsize=(5 * n, 8), squeeze=False)
    title = args.title or f"{variable}: A vs B"
    fig.suptitle(title)

    for row, (ds, da, td, label) in enumerate(
        [
            (ds_a, da_a, td_a, Path(args.a).name),
            (ds_b, da_b, td_b, Path(args.b).name),
        ]
    ):
        for col in range(n):
            ax = axes[row][col]
            sel = _panel_values(da, td, col)
            if _is_station(ds):
                lats = ds["latitude"].values
                lons = ds["longitude"].values
                im = ax.scatter(
                    lons,
                    lats,
                    c=sel.values,
                    cmap=args.colormap,
                    vmin=vmin,
                    vmax=vmax,
                    s=30,
                )
            else:
                lat_dim = _pick_dim(sel, LAT_ALIASES)
                lon_dim = _pick_dim(sel, LON_ALIASES)
                im = sel.transpose(lat_dim, lon_dim).plot.pcolormesh(
                    ax=ax,
                    x=lon_dim,
                    y=lat_dim,
                    cmap=args.colormap,
                    vmin=vmin,
                    vmax=vmax,
                    add_colorbar=False,
                )
            ax.set_title(f"{label} [{td}={col}]", fontsize=9)
            if col != 0:
                ax.set_ylabel("")
            if row != 1:
                ax.set_xlabel("")

    fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.7, pad=0.02, label=variable)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
