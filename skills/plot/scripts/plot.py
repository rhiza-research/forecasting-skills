# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "cartopy",
#   "cf-xarray",
#   "cftime",
#   # matplotlib<3.10: cartopy gridliner crash
#   "matplotlib>=3.8,<3.10",
#   "nc-time-axis",
#   "numpy",
#   "shapely>=2.1",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""Render a heatmap or timeseries PNG from a weather-skills standard dataset Zarr."""

import json
import re
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.standard_utils import lat_slice, polygon_from_geojson
from weather_skills_core.units import classify_variable, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_INDEX_INT_RE = re.compile(r"[+-]?[0-9]+")

# ECMWF-S2S4AFRICA / Kenya product palette (get_ECMWF_functions.cmap).
PRECIP_COLORS = [
    "white",
    "wheat",
    "lightgreen",
    "green",
    "lightblue",
    "blue",
    "yellow",
    "orange",
    "red",
    "purple",
]

def _parse_index(spec):
    """Parse ``--index`` into ``{dim: int | list[int]}`` (e.g. ``step=0,1,2``)."""
    if not spec or not spec.strip():
        return {}
    values = {}
    current = None
    for token in spec.split(","):
        if "=" in token:
            key, _, raw = token.partition("=")
            current = key.strip()
            if not current:
                raise ValueError(f"--index token {token.strip()!r} has an empty dimension name")
            if current in values:
                raise ValueError(f"--index dimension {current!r} is given more than once")
            values[current] = []
            raw = raw.strip()
            if not raw:
                raise ValueError(f"--index value for {current!r} is empty")
        else:
            raw = token.strip()
            if not raw:
                raise ValueError("--index spec has an empty token (stray comma)")
            if current is None:
                raise ValueError(f"--index token {raw!r} appears before any 'dim=' assignment")
        if not _INDEX_INT_RE.fullmatch(raw):
            raise ValueError(f"--index value {raw!r} for {current!r} is not an integer")
        pos = int(raw)
        if pos in values[current]:
            raise ValueError(f"--index position {pos} is repeated for dimension {current!r}")
        values[current].append(pos)
    return {k: v[0] if len(v) == 1 else v for k, v in values.items()}

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
    if spec is None or "," not in spec:
        return spec
    from matplotlib.colors import LinearSegmentedColormap

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    return LinearSegmentedColormap.from_list("custom", parts)


def _precip_colormap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("wgbrp", PRECIP_COLORS)


def _heatmap_cmap(da, colormap):
    """Explicit ``--colormap``, else the Kenya/S2S precip palette, else viridis."""
    if colormap:
        return _parse_colormap(colormap)
    kind = classify_variable(
        da.name or "",
        units=da.attrs.get("units"),
        standard_name=da.attrs.get("standard_name"),
    )
    if kind in ("precip", "precip_amount"):
        return _precip_colormap()
    return "viridis"

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
    cf = cf_dim(da, "time")
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


def _timeseries_axis(da, sdim):
    """X coord and xlabel. Forecast ``step`` (timedelta) + scalar init → valid times."""
    import numpy as np

    if sdim != "step" or "time" not in da.coords:
        return da[sdim].values, sdim
    time_coord = da["time"]
    if getattr(time_coord, "ndim", 1) != 0:
        return da[sdim].values, sdim
    steps = np.asarray(da["step"].values)
    if steps.dtype.kind != "m":
        return da[sdim].values, sdim
    init = np.asarray(time_coord.values)
    if init.dtype.kind != "M":
        return da[sdim].values, sdim
    return (init + steps).astype("datetime64[ns]"), "valid time"

def _panel_title(da, sdim, step_value, all_steps):
    """'<start> until <end>' from time + step; else '<sdim>=<step>'."""
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
    except Exception:  # noqa: BLE001
        return fallback

def _heatmap(
    da,
    lat_dim,
    lon_dim,
    cmap,
    extent,
    cities,
    title,
    fontsize,
    wrap_lon=True,
    native_step_dim=None,
    native_steps=None,
):
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import matplotlib.pyplot as plt
    import numpy as np

    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)

    if wrap_lon:
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

    title_steps = (
        native_steps if native_steps is not None and native_step_dim == sdim else steps
    )

    num_steps = len(steps)
    ncols = min(4, num_steps)
    nrows = int(np.ceil(num_steps / ncols))

    if extent is None:
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
        slab = da if sdim is None else da.isel({sdim: i})
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
        if wrap_lon:
            ax.set_extent(extent, crs=ccrs.PlateCarree())
        else:
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
        ax.add_feature(cfeature.COASTLINE, edgecolor="black")
        ax.add_feature(cfeature.BORDERS, linestyle=":", alpha=0.7)
        gl = ax.gridlines(draw_labels=True, alpha=0)
        gl.top_labels = False
        gl.right_labels = False
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        for city, (lat, lon) in cities.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=ccrs.PlateCarree())
            ax.text(lon - 2.0, lat + 0.5, city, fontsize=10, transform=ccrs.PlateCarree())
        if s is not None:
            ax.set_title(_panel_title(da, sdim, s, title_steps), fontsize=int(fontsize * 0.8))

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    if title:
        fig.suptitle(title, fontsize=fontsize)
    fig.tight_layout(rect=[0, 0, 1, 0.94] if title else None)
    cbar_ax = fig.add_axes([0.15, -0.04, 0.7, 0.01 + 0.02 / nrows])
    cbar = fig.colorbar(contour, cax=cbar_ax, orientation="horizontal", fraction=5)
    units = da.attrs.get("units", "")
    label = da.attrs.get("GRIB_name") or da.attrs.get("long_name") or da.name or "value"
    if units:
        label = f"{label} [{units}]"
    cbar.set_label(label, fontsize=fontsize)
    return fig

@weather_skill(
    name="plot",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset('any'), required=True, dest='ds')
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--style", choices=["heatmap", "timeseries"], default="heatmap")
@weather_skill.argument(
            "--colormap",
            default=None,
            help=(
                "matplotlib colormap name, or comma-separated colors. "
                "Default: Kenya/S2S precip palette for precip variables, else viridis."
            ),
        )
@weather_skill.argument(
            "--index",
            default=None,
            help="Slice like 'step=3,number=0' (heatmap only). Lists keep the dim as panels.",
        )
@weather_skill.argument(
            "--extent",
            default=None,
            help="Map extent 'lon_min,lon_max,lat_min,lat_max' (heatmap only).",
        )
@weather_skill.argument(
            "--cities",
            default=None,
            help='City overlay JSON (heatmap only). Inline {"name": [lat, lon]} or file path.',
        )
@weather_skill.argument("--fontsize", type=int, default=16)
@weather_skill.argument("--title", default=None, help="Optional plot title.")
@weather_skill.argument(
            "--mask-geojson",
            default=None,
            help="GeoJSON polygon; cells outside become NaN (heatmap only).",
        )
def plot(
    ds,
    bbox,
    variable,
    style,
    colormap,
    title,
    index,
    extent,
    cities,
    fontsize,
    mask_geojson,
    output,
    **kwargs,
):
    """Render a heatmap or timeseries PNG from a weather-skills standard dataset Zarr."""
    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter
    import xarray as xr

    variable = variable or auto_variable(ds)
    if not variable or variable not in ds:
        raise UsageError(f"no usable variable. Available: {list(ds.data_vars)}")
    ds = to_standard_units(ds, variables=[variable])
    da = ds[variable]
    try:
        overrides = _parse_index(index)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    bbox_nwse = bbox
    heatmap_only = {
        "--bbox": bbox_nwse is not None,
        "--mask-geojson": bool(mask_geojson),
        "--extent": bool(extent),
        "--cities": bool(cities),
        "--index": bool(overrides),
    }
    if style != "heatmap":
        for flag, set_ in heatmap_only.items():
            if not set_:
                continue
            detail = f" {bbox_nwse[0]}/{bbox_nwse[1]}/{bbox_nwse[2]}/{bbox_nwse[3]}" if flag == "--bbox" else (
                f" {extent!r}" if flag == "--extent" else (f" {index!r}" if flag == "--index" else "")
            )
            print(
                f"Warning: {flag}{detail} is a heatmap-only option; ignored for --style {style}.",
                file=sys.stderr,
            )

    if style == "heatmap":
        lat_dim = cf_dim(da, "latitude")
        lon_dim = cf_dim(da, "longitude")
        if lat_dim is None or lon_dim is None:
            raise UsageError(f"heatmap requires lat/lon coords; got {list(da.dims)}.")
        if lat_dim not in da.dims or lon_dim not in da.dims:
            raise UsageError(
                f"heatmap needs lat/lon as dimensions, but {lat_dim!r}/"
                f"{lon_dim!r} are non-dimension coordinates here (dims: "
                f"{list(da.dims)}); station data has no 2D grid to plot."
            )
        native_step_dim = _step_dim(da)
        native_steps = list(da[native_step_dim].values) if native_step_dim else None

        for dim, idx in overrides.items():
            if dim not in da.dims:
                raise UsageError(
                    f"--index dimension {dim!r} is not in the data (dims: {list(da.dims)})"
                )
            if isinstance(idx, list) and dim != native_step_dim:
                panel_desc = repr(native_step_dim) if native_step_dim else "step/time"
                raise UsageError(
                    f"--index list selection on {dim!r} is only supported "
                    f"on the panel dimension ({panel_desc}); give a single position"
                )
        for dim, idx in overrides.items():
            size = da.sizes[dim]
            positions = idx if isinstance(idx, list) else [idx]
            seen = {}
            for pos in positions:
                if not -size <= pos < size:
                    raise UsageError(
                        f"--index position {pos} is out of range for "
                        f"dimension {dim!r} (size {size})"
                    )
                norm = pos % size
                if norm in seen:
                    raise UsageError(
                        f"--index positions {seen[norm]} and {pos} address "
                        f"the same element of dimension {dim!r} (size {size})"
                    )
                seen[norm] = pos
            da = da.isel({dim: idx}, drop=True)

        for spatial in (lat_dim, lon_dim):
            if spatial in overrides and spatial not in da.dims:
                raise UsageError(
                    f"--index removed the {spatial!r} dimension; heatmap needs a 2D lat/lon grid"
                )
        panel_dim = _step_dim(da)
        for dim in da.dims:
            if dim not in (panel_dim, "number", lat_dim, lon_dim):
                panel_desc = repr(panel_dim) if panel_dim else "step/time"
                raise UsageError(
                    f"dimension {dim!r} remains after selection; heatmap "
                    f"panels only the {panel_desc} dimension — select a position "
                    f"from {dim!r} with --index"
                )
        if panel_dim is not None and da.sizes[panel_dim] == 0:
            raise UsageError(f"dimension {panel_dim!r} has size 0; nothing to plot.")

        extent_vals = _parse_extent(extent)
        cities_map = _parse_cities(cities)
        cmap = _heatmap_cmap(da, colormap)
        region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
        wrapped_bbox = bbox_nwse is not None and bbox_nwse[1] > bbox_nwse[3]

        if bbox_nwse is not None or region_polygon is not None:
            import numpy as np

            lon_vals_pre = np.asarray(da[lon_dim].values)
            if lon_vals_pre.size and float(np.nanmax(lon_vals_pre)) > 180.0:
                da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)
            if bbox_nwse is not None:
                r_n, r_w, r_s, r_e = bbox_nwse
                da = da.sel({lat_dim: lat_slice(da[lat_dim].values, r_n, r_s)})
                if r_w > r_e:
                    da = da.where((da[lon_dim] >= r_w) | (da[lon_dim] <= r_e), drop=True)
                else:
                    da = da.sel({lon_dim: slice(r_w, r_e)})
            if region_polygon is not None:
                import shapely

                lon_grid, lat_grid = np.meshgrid(da[lon_dim].values, da[lat_dim].values)
                mask = shapely.contains_xy(region_polygon, lon_grid, lat_grid)
                if not bool(mask.any()):
                    print(
                        "Warning: --mask-geojson polygon does not intersect the grid; "
                        "the map will be entirely empty.",
                        file=sys.stderr,
                    )
                da = da.where(xr.DataArray(mask, dims=(lat_dim, lon_dim)))
            if bbox_nwse is not None:
                r_n, r_w, r_s, r_e = bbox_nwse
                if r_w > r_e:
                    shifted = ((da[lon_dim] - r_w) % 360.0) + r_w
                    da = da.assign_coords({lon_dim: shifted}).sortby(lon_dim)
                if extent_vals is None:
                    if r_w > r_e:
                        extent_vals = [float(r_w), float(r_e) + 360.0, float(r_s), float(r_n)]
                    else:
                        extent_vals = [float(r_w), float(r_e), float(r_s), float(r_n)]

        if da.sizes[lat_dim] == 0 or da.sizes[lon_dim] == 0:
            raise UsageError(
                "selection produced an empty grid (no cells remain after "
                "--index/--bbox selection); nothing to plot."
            )
        fig = _heatmap(
            da,
            lat_dim,
            lon_dim,
            cmap,
            extent_vals,
            cities_map,
            title,
            fontsize,
            wrap_lon=not wrapped_bbox,
            native_step_dim=native_step_dim,
            native_steps=native_steps,
        )
    else:
        fig, ax = plt.subplots(figsize=(10, 6))
        sdim = "step" if "step" in da.dims else cf_dim(da, "time")
        if sdim is None:
            raise UsageError(f"timeseries needs 'step' or 'time'; got {list(da.dims)}.")
        reduce_dims = [d for d in da.dims if d != sdim]
        reduced = da.mean(reduce_dims, keep_attrs=True)
        xvals, xlabel = _timeseries_axis(reduced, sdim)
        ax.plot(xvals, reduced.values)
        ylabel = reduced.attrs.get("long_name") or variable
        units = reduced.attrs.get("units")
        if units:
            ylabel = f"{ylabel} [{units}]"
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title or f"{variable} ({style})")
        if xlabel == "valid time":
            fig.autofmt_xdate()
        fig.tight_layout()

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output

if __name__ == "__main__":
    plot()
