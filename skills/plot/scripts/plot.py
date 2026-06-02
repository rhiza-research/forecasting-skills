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
import hashlib
import json
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.1"

# Region bbox table accepted by ``--region``. Mirrors ``clip-region``'s
# REGIONS dict; duplicated per CONVENTIONS.md (no shared helper module —
# skills stay standalone). Tuples are (N, W, S, E) in decimal degrees.
REGIONS = {
    "africa": (23, -20, -37, 59),
    "kenya": (7, 32, -6, 43),
    "ghana": (12, -4, 4, 2),
    "senegal": (17, -17.5, 12, -11),
    "ethiopia": (16, 32, 2, 49),
    "namibia": (-15, 10, -31, 27),
    "botswana": (-15, 18, -28, 31),
    "zambia": (-6, 20, -20, 35),
    "madagascar": (-10, 42, -27, 52),
    "angola": (-5, 12, -18, 24),
}


def _lat_slice(lat_vals, north, south):
    """Return a ``slice`` for ``ds.sel`` that works for ascending or descending lat."""
    if lat_vals.size and lat_vals[0] > lat_vals[-1]:
        return slice(north, south)
    return slice(south, north)


def _hash_zarr(zarr_path: Path) -> str:
    """Stable content hash of a zarr's stored bytes. Walks the zarr dir
    deterministically and hashes relative-path bytes + each file's
    content. Returns sha256 hex digest."""
    h = hashlib.sha256()
    for p in sorted(zarr_path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(zarr_path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _load_history(zarr_path: Path) -> list:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            raw = ds.attrs.get("rhiza_history")
    except FileNotFoundError:
        # A not-yet-existing output read during a cache check is a silent miss.
        return []
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list):
        # A present-but-non-array value is malformed under the rhiza_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed rhiza_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


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


def _parse_colormap(spec):
    """Pass through a named matplotlib colormap, or build one from a color list.

    Named matplotlib colormap identifiers cannot contain commas, so the
    presence of a comma in ``spec`` unambiguously indicates a custom color
    list. Whitespace around each color is stripped.
    """
    if spec is None or "," not in spec:
        return spec
    from matplotlib.colors import LinearSegmentedColormap

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    return LinearSegmentedColormap.from_list("custom", parts)


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
        da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)

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
        # Default extent expands cell-center min/max by half the mean grid
        # spacing on each side, so the view matches what pcolormesh actually
        # draws (it treats coords as cell centers and extends ±½ spacing).
        lat_vals = np.asarray(da[lat_dim].values)
        lon_vals = np.asarray(da[lon_dim].values)
        dlat = float(np.abs(np.diff(np.sort(lat_vals))).mean()) if lat_vals.size > 1 else 0.0
        dlon = float(np.abs(np.diff(np.sort(lon_vals))).mean()) if lon_vals.size > 1 else 0.0
        extent = [
            float(lon_vals.min()) - dlon / 2,
            float(lon_vals.max()) + dlon / 2,
            float(lat_vals.min()) - dlat / 2,
            float(lat_vals.max()) + dlat / 2,
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
        # Order the 2D field as (lat, lon) so its shape matches the C array
        # pcolormesh expects given the (lon, lat) X/Y coordinate vectors.
        # Sources like IMERG store the variable as (..., lon, lat), which
        # would otherwise feed a transposed array and raise a shape mismatch.
        # Mirrors plot-compare's _grid_panel transpose.
        slab = slab.transpose(lat_dim, lon_dim)
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

    if title:
        fig.suptitle(title, fontsize=fontsize)
    fig.tight_layout(rect=[0, 0, 1, 0.94] if title else None)
    cbar_ax = fig.add_axes([0.15, -0.04, 0.7, 0.01 + 0.02 / nrows])
    cbar = fig.colorbar(contour, cax=cbar_ax, orientation="horizontal", fraction=5)
    units = da.attrs.get("units", "")
    label = da.attrs.get("long_name") or da.attrs.get("GRIB_name") or da.name or "value"
    if units:
        label = f"{label} [{units}]"
    cbar.set_label(label, fontsize=fontsize)

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
    p.add_argument(
        "--region",
        choices=sorted(REGIONS),
        help="Named region. Slices the gridded input to the region's "
        "(N, W, S, E) bbox and sets the axes extent to that bbox. Cells "
        "inside the bbox but outside the country polygon are kept "
        "(rectangular slice, matching the upstream convention).",
    )
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

    if args.region and args.style != "heatmap":
        print(
            f"Warning: --region {args.region} is a heatmap-only option; "
            f"ignored for --style {args.style}.",
            file=sys.stderr,
        )

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
        cmap = _parse_colormap(args.colormap)
        if args.region:
            import numpy as _np_local

            r_n, r_w, r_s, r_e = REGIONS[args.region]
            # Wrap 0..360 lons to [-180, 180] before slicing so a global
            # grid still intersects regions in the negative-lon half (e.g.
            # Senegal). Mirror plot-compare's pre-slice wrap.
            lon_vals_pre = _np_local.asarray(da[lon_dim].values)
            if lon_vals_pre.size and float(_np_local.nanmax(lon_vals_pre)) > 180.0:
                da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)
            lat_vals_pre = da[lat_dim].values
            da = da.sel({lat_dim: _lat_slice(lat_vals_pre, r_n, r_s), lon_dim: slice(r_w, r_e)})
            if extent is None:
                extent = [float(r_w), float(r_e), float(r_s), float(r_n)]
        fig = _heatmap(
            da,
            lat_dim,
            lon_dim,
            cmap,
            extent,
            cities,
            args.title,
            args.fontsize,
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

    upstream = _load_history(src)
    plot_entry = {
        "skill": "plot",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name, "hash": _hash_zarr(src)},
    }
    if not upstream:
        print(
            f"Warning: no upstream rhiza_history on {src.name}; embedding plot step alone.",
            file=sys.stderr,
        )
    fig.savefig(
        out,
        dpi=150,
        bbox_inches="tight",
        metadata={
            "rhiza_history": json.dumps(upstream + [plot_entry], sort_keys=True),
            "Software": "forecasting-skills",
        },
    )
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
