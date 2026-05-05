# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cartopy",
#   "cf-xarray",
#   "matplotlib",
#   "numpy",
#   "xarray",
#   "zarr",
# ]
# ///
"""Render a heatmap or timeseries PNG from a Rhiza Envelope Zarr.

The heatmap mode produces a CartoPy panel layout: one subplot per step
(up to 4 columns), shared color scale, country/coastline boundaries, and a
horizontal colorbar at the bottom spanning all panels.
"""

import argparse
import json
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


def _parse_extent(spec):
    if not spec:
        return None
    parts = [float(x) for x in spec.split(",")]
    if len(parts) != 4:
        raise ValueError(
            "--extent expects 4 comma-separated floats: lon_min,lon_max,lat_min,lat_max"
        )
    return parts


def _parse_cities(spec):
    if not spec:
        return {}
    p = Path(spec)
    raw = p.read_text() if p.exists() else spec
    data = json.loads(raw)
    out = {}
    for name, val in data.items():
        if isinstance(val, dict):
            out[name] = (float(val["lat"]), float(val["lon"]))
        else:
            out[name] = (float(val[0]), float(val[1]))
    return out


def _figsize_from_extent(lon_min, lon_max, lat_min, lat_max, base_height=5.0):
    lat_range = abs(lat_max - lat_min)
    lon_range = abs(lon_max - lon_min)
    if lat_range == 0 or lon_range == 0:
        return base_height, base_height
    height = base_height
    width = height * lon_range / lat_range
    return max(width, 2.0), height


def _step_dim(da):
    for cand in ("step", "time", "valid_time"):
        if cand in da.dims:
            return cand
    cf = _cf_dim(da, "time")
    return cf if cf and cf in da.dims else None


def _format_step(value):
    import numpy as np

    arr = np.asarray(value)
    if arr.dtype.kind == "M":
        return str(arr)[:16]
    if arr.dtype.kind == "m":
        days = arr.astype("timedelta64[D]").astype(int)
        return f"+{days}d"
    return str(value)


def _panel_title(da, sdim, step_value, all_steps):
    """Match panel_plot_variable: '<start> until <end>' from time + step.

    Falls back to '<sdim>=<step>' when the dataset lacks the coords needed
    to construct a date range (no `time` coord, or `step` not a timedelta).
    """
    import numpy as np

    fallback = f"{sdim}={_format_step(step_value)}"
    if "time" not in da.coords:
        return fallback
    step_arr = np.asarray(all_steps)
    if step_arr.dtype.kind != "m":
        return fallback
    try:
        time_val = np.asarray(da["time"].values)
        first = step_arr[0]
        if step_value == first:
            start = time_val
            end = time_val + np.asarray(step_value)
        else:
            dt = step_arr[1] - step_arr[0] if step_arr.size > 1 else np.asarray(step_value) - first
            end = time_val + np.asarray(step_value)
            start = end - dt
        return f"{str(start)[:16]} until {str(end)[:16]}"
    except Exception:
        return fallback


def _heatmap(da, lat_dim, lon_dim, cmap, extent, cities, title, fontsize):
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import matplotlib.pyplot as plt
    import numpy as np

    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)

    lon_vals = np.asarray(da[lon_dim].values)
    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
        da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(
            lon_dim
        )

    sdim = _step_dim(da)
    if sdim is None or da.sizes.get(sdim, 1) == 1:
        if sdim and sdim in da.dims:
            da = da.squeeze(sdim, drop=True)
        steps = [None]
        sdim = None
    else:
        steps = list(da[sdim].values)

    num_steps = len(steps)
    ncols = min(4, num_steps)
    nrows = int(np.ceil(num_steps / ncols))

    if extent is None:
        extent = [
            float(np.min(da[lon_dim].values)),
            float(np.max(da[lon_dim].values)),
            float(np.min(da[lat_dim].values)),
            float(np.max(da[lat_dim].values)),
        ]

    vmax = float(da.max(skipna=True).values)
    vmin = float(da.min(skipna=True).values)
    if vmax > 0 and vmin < 0:
        m = max(abs(vmax), abs(vmin))
        vmin, vmax = -m, m

    sw, sh = _figsize_from_extent(*extent)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(sw * ncols, sh * nrows),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
    )
    axes = np.array(axes).reshape(nrows, ncols).flatten()

    contour = None
    for i, s in enumerate(steps):
        ax = axes[i]
        slab = da if s is None else da.sel({sdim: s})
        contour = ax.pcolormesh(
            slab[lon_dim],
            slab[lat_dim],
            slab.values,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            transform=ccrs.PlateCarree(),
        )
        ax.set_extent(extent, crs=ccrs.PlateCarree())
        ax.add_feature(cfeature.COASTLINE, edgecolor="black")
        ax.add_feature(cfeature.BORDERS, linestyle=":", alpha=0.7)
        gl = ax.gridlines(draw_labels=True, alpha=0)
        gl.top_labels = False
        gl.right_labels = False
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        for city, (lat, lon) in cities.items():
            ax.plot(
                lon,
                lat,
                marker="o",
                color="k",
                markersize=6,
                transform=ccrs.PlateCarree(),
            )
            ax.text(
                lon - 2.0,
                lat + 0.5,
                city,
                fontsize=10,
                transform=ccrs.PlateCarree(),
            )
        if s is not None:
            ax.set_title(
                _panel_title(da, sdim, s, steps),
                fontsize=int(fontsize * 0.8),
            )

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    fig.tight_layout()
    cbar_ax = fig.add_axes([0.15, -0.04, 0.7, 0.01 + 0.02 / nrows])
    cbar = fig.colorbar(
        contour, cax=cbar_ax, orientation="horizontal", fraction=5
    )
    units = da.attrs.get("units", "")
    label = da.name or "value"
    if units:
        label = f"{label} [{units}]"
    cbar.set_label(label, fontsize=fontsize)

    if title:
        fig.suptitle(title, fontsize=fontsize)

    return fig


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--variable", "-v")
    p.add_argument("--style", choices=["heatmap", "timeseries"], default="heatmap")
    p.add_argument("--colormap", default="viridis")
    p.add_argument("--title")
    p.add_argument("--index", help="Slice spec like 'step=3,number=0'")
    p.add_argument(
        "--extent",
        help="Map extent as 'lon_min,lon_max,lat_min,lat_max' (heatmap only).",
    )
    p.add_argument(
        "--cities",
        help='City overlay JSON (heatmap only). Inline {"name": [lat, lon]} or path to a JSON file.',
    )
    p.add_argument("--fontsize", type=int, default=16)
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

    if args.style == "heatmap":
        lat_dim = _cf_dim(da, "latitude")
        lon_dim = _cf_dim(da, "longitude")
        if lat_dim is None or lon_dim is None:
            print(
                f"Error: heatmap requires lat/lon coords; got {list(da.dims)}.",
                file=sys.stderr,
            )
            sys.exit(2)
        for dim, idx in overrides.items():
            if dim in da.dims:
                da = da.isel({dim: idx}, drop=True)
        extent = _parse_extent(args.extent)
        cities = _parse_cities(args.cities)
        fig = _heatmap(
            da, lat_dim, lon_dim, args.colormap, extent, cities, args.title, args.fontsize
        )
    else:
        fig, ax = plt.subplots(figsize=(10, 6))
        sdim = "step" if "step" in da.dims else _cf_dim(da, "time")
        if sdim is None:
            print(
                f"Error: timeseries needs 'step' or 'time'; got {list(da.dims)}.",
                file=sys.stderr,
            )
            sys.exit(2)
        reduce_dims = [d for d in da.dims if d != sdim]
        da.mean(reduce_dims).plot(ax=ax)
        ax.set_xlabel(sdim)
        ax.set_title(args.title or f"{variable} ({args.style})")
        fig.tight_layout()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
