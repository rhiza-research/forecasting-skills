# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
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
"""Render a heatmap, timeseries, wind-rose, or quiver PNG from a weather-skills standard dataset Zarr."""

import json
import re
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.standard_utils import lat_slice, parse_bbox, polygon_from_geojson
from weather_skills_core.units import (
    classify_variable,
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_label_for_display,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

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
PRECIP_BOUNDS = [0, 10, 20, 40, 60, 80, 110, 150, 200, 250, 350]

# Meteorological wind rose: 16 compass sectors, speed stacked in m/s classes.
WIND_ROSE_SECTORS = 16
WIND_SPEED_EDGES_MS = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
WIND_SPEED_COLORS = [
    "#c6dbef",
    "#6baed6",
    "#2171b5",
    "#08306b",
    "#fd8d3c",
    "#d94801",
    "#7f2704",
]
_UV_NAME_PAIRS = (
    ("u10", "v10"),
    ("u100", "v100"),
    ("10u", "10v"),
    ("uas", "vas"),
    ("ua", "va"),
    ("u", "v"),
    ("eastward_wind", "northward_wind"),
    ("10m_u_component_of_wind", "10m_v_component_of_wind"),
    ("100m_u_component_of_wind", "100m_v_component_of_wind"),
    ("u_component_of_wind", "v_component_of_wind"),
    ("uwind", "vwind"),
    ("uwnd", "vwnd"),
)
_SAMPLE_DIM_NAMES = {"step", "number", "point_id", "station_id", "valid_time"}

# ECMWF-S2S4AFRICA quiver_plot_variable (plot_s2s 10 m / 700 hPa wind vectors).
QUIVER_CMAP = "YlGn"
QUIVER_SCALE = 40.0
QUIVER_REGRID_SHAPE = 10
QUIVER_KEY_MS = (5.0, 10.0)

# Natural Earth scale vs map span (max of lon/lat extent in degrees).
# Admin-1 (states / provinces / counties) is only readable on country-to-regional
# views; a continental or global map would be a thicket of province lines.
_ADMIN1_MAX_SPAN_DEG = 45.0
_HIRES_MAX_SPAN_DEG = 90.0

_ADMIN1_STYLE = {"facecolor": "none", "edgecolor": "0.45", "linewidth": 0.4, "zorder": 3}
_LAKES_STYLE = {"facecolor": "none", "edgecolor": "0.2", "linewidth": 0.5, "zorder": 3.5}
_BORDERS_STYLE = {"facecolor": "none", "edgecolor": "0.15", "linewidth": 0.8, "zorder": 4}
_COAST_STYLE = {"facecolor": "none", "edgecolor": "black", "linewidth": 0.8, "zorder": 4}


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


def _apply_index(da, overrides, *, list_dims=()):
    """Select integer positions from ``overrides`` (``--index``).

    A dim in ``list_dims`` may keep several positions; any other dim must be a
    single position (the dim is then dropped). Pass ``list_dims=None`` to allow
    a list on every dim. Duplicate / out-of-range positions (including negative
    aliases of the same element) are errors.
    """
    if not overrides:
        return da
    allow_all_lists = list_dims is None
    list_dims = () if list_dims is None else list_dims
    for dim, idx in overrides.items():
        if dim not in da.dims:
            raise UsageError(
                f"--index dimension {dim!r} is not in the data (dims: {list(da.dims)})"
            )
        if isinstance(idx, list) and not allow_all_lists and dim not in list_dims:
            panel_desc = ", ".join(repr(d) for d in list_dims) if list_dims else "step/time"
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
                    f"--index position {pos} is out of range for dimension {dim!r} (size {size})"
                )
            norm = pos % size
            if norm in seen:
                raise UsageError(
                    f"--index positions {seen[norm]} and {pos} address "
                    f"the same element of dimension {dim!r} (size {size})"
                )
            seen[norm] = pos
        da = da.isel({dim: idx}, drop=True)
    return da


def _subset_spatial(da, lat_dim, lon_dim, bbox_nwse, region_polygon, extent_vals):
    """Slice ``da`` to ``--bbox`` / ``--mask-geojson``. Returns ``(da, extent_vals)``."""
    if bbox_nwse is None and region_polygon is None:
        return da, extent_vals
    import numpy as np
    import xarray as xr

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
    return da, extent_vals


def _subset_points(da, bbox_nwse, region_polygon):
    """Filter station / point samples to ``--bbox`` / ``--mask-geojson``."""
    import numpy as np
    import xarray as xr

    lat_name = cf_dim(da, "latitude")
    lon_name = cf_dim(da, "longitude")
    if lat_name is None or lon_name is None:
        raise UsageError(
            f"--bbox/--mask-geojson need latitude/longitude coordinates; got dims {list(da.dims)}"
        )
    lat = np.asarray(da[lat_name].values)
    lon = np.asarray(da[lon_name].values)
    keep = np.ones(np.broadcast(lat, lon).shape, dtype=bool)
    lat_b, lon_b = np.broadcast_arrays(lat, lon)
    if bbox_nwse is not None:
        r_n, r_w, r_s, r_e = bbox_nwse
        keep &= (lat_b >= r_s) & (lat_b <= r_n)
        if r_w > r_e:
            keep &= (lon_b >= r_w) | (lon_b <= r_e)
        else:
            keep &= (lon_b >= r_w) & (lon_b <= r_e)
    if region_polygon is not None:
        import shapely

        keep &= shapely.contains_xy(region_polygon, lon_b, lat_b)
        if not bool(keep.any()):
            print(
                "Warning: --mask-geojson polygon does not intersect the points; "
                "the rose will be empty.",
                file=sys.stderr,
            )
    keep_da = xr.DataArray(keep, dims=da[lat_name].dims)
    return da.where(keep_da, drop=True)


def _prepare_gridded_map(da, overrides, bbox_nwse, mask_geojson, extent, *, style):
    """Index, bbox, and mask a lat/lon field for a map panel. Returns a tuple.

    ``(da, lat_dim, lon_dim, extent_vals, wrap_lon, native_step_dim, native_steps)``.
    """
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if lat_dim is None or lon_dim is None:
        raise UsageError(f"{style} requires lat/lon coords; got {list(da.dims)}.")
    if lat_dim not in da.dims or lon_dim not in da.dims:
        raise UsageError(
            f"{style} needs lat/lon as dimensions, but {lat_dim!r}/"
            f"{lon_dim!r} are non-dimension coordinates here (dims: "
            f"{list(da.dims)}); station data has no 2D grid to plot."
        )
    native_step_dim = _step_dim(da)
    native_steps = list(da[native_step_dim].values) if native_step_dim else None
    list_dims = (native_step_dim,) if native_step_dim else ()
    da = _apply_index(da, overrides, list_dims=list_dims)
    for spatial_dim in (lat_dim, lon_dim):
        if spatial_dim in overrides and spatial_dim not in da.dims:
            raise UsageError(
                f"--index removed the {spatial_dim!r} dimension; {style} needs a 2D lat/lon grid"
            )
    panel_dim = _step_dim(da)
    for dim in da.dims:
        if dim not in (panel_dim, "number", lat_dim, lon_dim):
            panel_desc = repr(panel_dim) if panel_dim else "step/time"
            raise UsageError(
                f"dimension {dim!r} remains after selection; {style} "
                f"panels only the {panel_desc} dimension — select a position "
                f"from {dim!r} with --index"
            )
    if panel_dim is not None and da.sizes[panel_dim] == 0:
        raise UsageError(f"dimension {panel_dim!r} has size 0; nothing to plot.")
    extent_vals = _parse_extent(extent)
    region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
    wrapped_bbox = bbox_nwse is not None and bbox_nwse[1] > bbox_nwse[3]
    da, extent_vals = _subset_spatial(da, lat_dim, lon_dim, bbox_nwse, region_polygon, extent_vals)
    if da.sizes[lat_dim] == 0 or da.sizes[lon_dim] == 0:
        raise UsageError(
            "selection produced an empty grid (no cells remain after "
            "--index/--bbox selection); nothing to plot."
        )
    return da, lat_dim, lon_dim, extent_vals, not wrapped_bbox, native_step_dim, native_steps


def _plain(da):
    """Drop a pint wrapper so matplotlib sees a numpy array."""
    if getattr(da.pint, "units", None) is not None:
        return da.pint.dequantify()
    return da


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


def _flag_values(da):
    """Sorted CF ``flag_values``, or None."""
    import numpy as np

    raw = da.attrs.get("flag_values")
    if raw is None:
        return None
    values = np.asarray(raw, dtype=float).ravel()
    if values.size < 2:
        return None
    return np.sort(values)


def _discrete_flag_scale(da, colormap):
    """ListedColormap + BoundaryNorm for CF flag fields, or None."""
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap

    values = _flag_values(da)
    if values is None:
        return None
    meanings = da.attrs.get("flag_meanings")
    labels = None
    if isinstance(meanings, str) and meanings.strip():
        parts = meanings.split()
        raw = np.asarray(da.attrs.get("flag_values"), dtype=float).ravel()
        if parts and len(parts) == raw.size:
            labels = [parts[i] for i in np.argsort(raw)]
    colors = None
    if colormap and "," in colormap:
        parts = [p.strip() for p in colormap.split(",") if p.strip()]
        if len(parts) == values.size:
            colors = parts
    if colors is None:
        if values.size == 3:
            colors = ["#d73027", "#f0f0f0", "#1a9850"]
        else:
            from matplotlib import colormaps

            tab = colormaps["tab10"](np.linspace(0, 1, values.size))
            colors = [tuple(c) for c in tab]
    mids = (values[:-1] + values[1:]) / 2.0
    bounds = np.concatenate(([values[0] - 0.5], mids, [values[-1] + 0.5]))
    cmap = ListedColormap(colors)
    return cmap, BoundaryNorm(bounds, cmap.N), values, labels


def _is_precip(da):
    kind = classify_variable(
        da.name or "",
        units=variable_units(da),
        standard_name=da.attrs.get("standard_name"),
    )
    return kind in ("precip", "precip_amount")


def _precip_scale():
    """Discrete Kenya / S2S rainfall classes (ListedColormap + BoundaryNorm)."""
    from matplotlib.colors import BoundaryNorm, ListedColormap

    cmap = ListedColormap(PRECIP_COLORS, name="wgbrp")
    return cmap, BoundaryNorm(PRECIP_BOUNDS, ncolors=cmap.N, clip=True)


def _heatmap_scale(da, colormap):
    """Return ``(cmap, norm)``. ``norm`` is set for the default precip scale."""
    if colormap:
        return _parse_colormap(colormap), None
    if _is_precip(da):
        return _precip_scale()
    return "viridis", None


def _variable_label(da):
    """Colorbar / axis label from CF ``long_name`` (then GRIB_name, then the name)."""
    return variable_label_for_display(da)


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


def _parse_draw_boxes(specs):
    """Parse repeatable ``--draw-box N/W/S/E`` into a list of ``(N, W, S, E)``."""
    if not specs:
        return []
    boxes = []
    for spec in specs:
        try:
            boxes.append(parse_bbox(spec))
        except UsageError as exc:
            raise UsageError(
                f"--draw-box {spec!r} must be four decimal degrees N/W/S/E (same form as --bbox)."
            ) from exc
    return boxes


def _draw_boxes_on_ax(ax, boxes, transform):
    """Outline each N/W/S/E box in black (split antimeridian spans into two)."""
    from matplotlib.patches import Rectangle

    for north, west, south, east in boxes:
        height = north - south
        if west <= east:
            ax.add_patch(
                Rectangle(
                    (west, south),
                    east - west,
                    height,
                    fill=False,
                    edgecolor="black",
                    linewidth=1.5,
                    transform=transform,
                    zorder=5,
                )
            )
        else:
            # Antimeridian: west..180 and -180..east
            ax.add_patch(
                Rectangle(
                    (west, south),
                    180.0 - west,
                    height,
                    fill=False,
                    edgecolor="black",
                    linewidth=1.5,
                    transform=transform,
                    zorder=5,
                )
            )
            ax.add_patch(
                Rectangle(
                    (-180.0, south),
                    east - (-180.0),
                    height,
                    fill=False,
                    edgecolor="black",
                    linewidth=1.5,
                    transform=transform,
                    zorder=5,
                )
            )


def _extent_span_deg(extent):
    lon_min, lon_max, lat_min, lat_max = extent
    return max(abs(lon_max - lon_min), abs(lat_max - lat_min))


def _boundary_layers(extent):
    """Natural Earth scale and whether to overlay admin-1 for this view."""
    span = _extent_span_deg(extent)
    if span > _HIRES_MAX_SPAN_DEG:
        return {"scale": "110m", "admin1": False}
    if span > _ADMIN1_MAX_SPAN_DEG:
        return {"scale": "50m", "admin1": False}
    return {"scale": "10m", "admin1": True}


def _extent_clip_geom(extent):
    """Shapely clip geometry for ``lon_min,lon_max,lat_min,lat_max``.

    Antimeridian views store a continuous unwrapped lon (e.g. 170..190) which
    is split back into ``[-180, 180]`` pieces for Natural Earth intersection.
    """
    from shapely.geometry import box

    lon_min, lon_max, lat_min, lat_max = extent
    if lon_max > 180.0:
        return box(lon_min, lat_min, 180.0, lat_max).union(
            box(-180.0, lat_min, lon_max - 360.0, lat_max)
        )
    if lon_min > lon_max:
        return box(lon_min, lat_min, 180.0, lat_max).union(box(-180.0, lat_min, lon_max, lat_max))
    return box(lon_min, lat_min, lon_max, lat_max)


def _unwrap_geoms(geoms, lon_min):
    """Shift western-hemisphere pieces so they match an unwrapped lon axis."""
    import numpy as np
    import shapely

    def shift(coords):
        out = np.asarray(coords).copy()
        out[:, 0] = np.where(out[:, 0] < lon_min, out[:, 0] + 360.0, out[:, 0])
        return out

    shifted = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        shifted.append(shapely.transform(geom, shift))
    return shifted


def _clip_ne_geoms(resolution, category, name, clip_geom):
    """Natural Earth geometries intersecting ``clip_geom`` (eager download)."""
    import cartopy.io.shapereader as shpreader

    path = shpreader.natural_earth(resolution=resolution, category=category, name=name)
    geoms = []
    for geom in shpreader.Reader(path).geometries():
        if geom is None or geom.is_empty:
            continue
        try:
            if not geom.intersects(clip_geom):
                continue
            clipped = geom.intersection(clip_geom)
        except Exception:  # noqa: BLE001
            clipped = geom
        if clipped is None or clipped.is_empty:
            continue
        if clipped.geom_type == "GeometryCollection":
            geoms.extend(g for g in clipped.geoms if g is not None and not g.is_empty)
        else:
            geoms.append(clipped)
    return geoms


def _load_geo_overlays(extent):
    """Scale-appropriate coastline / border / lake / admin-1 overlays.

    Each layer is clipped to the map extent so a country-scale view does not
    draw the rest of the world. Download or clip failures warn and skip that
    layer — the heatmap still renders.
    """
    spec = _boundary_layers(extent)
    try:
        clip = _extent_clip_geom(extent)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: geographic overlays unavailable ({exc}); skipping.", file=sys.stderr)
        return []
    lon_min, lon_max = extent[0], extent[1]
    layers = []

    def add(category, name, style, resolution=None):
        res = resolution or spec["scale"]
        try:
            geoms = _clip_ne_geoms(res, category, name, clip)
        except Exception as exc:  # noqa: BLE001
            print(
                f"Warning: {name} overlay unavailable ({exc}); skipping.",
                file=sys.stderr,
            )
            return
        if lon_max > 180.0:
            geoms = _unwrap_geoms(geoms, lon_min)
        if geoms:
            layers.append((geoms, style))

    if spec["admin1"]:
        add("cultural", "admin_1_states_provinces", _ADMIN1_STYLE, resolution="10m")
    add("physical", "lakes", _LAKES_STYLE)
    add("cultural", "admin_0_boundary_lines_land", _BORDERS_STYLE)
    add("physical", "coastline", _COAST_STYLE)
    return layers


def _draw_geo_overlays(ax, overlays, crs):
    for geoms, style in overlays:
        ax.add_geometries(geoms, crs, **style)


def _panel_shape(n, rows=None, columns=None):
    """``(nrows, ncols)`` for ``n`` heatmap panels.

    Default: up to 4 columns, extra rows as needed (leftover cells stay blank).
    When ``rows`` and/or ``columns`` are set, the grid must pack ``n`` exactly.
    """
    if rows is not None and rows < 1:
        raise UsageError(f"--rows must be a positive integer; got {rows}")
    if columns is not None and columns < 1:
        raise UsageError(f"--columns must be a positive integer; got {columns}")
    if rows is None and columns is None:
        ncols = min(4, max(n, 1))
        nrows = (n + ncols - 1) // ncols if n else 1
        return nrows, ncols
    if rows is not None and columns is not None:
        product = rows * columns
        if product != n:
            raise UsageError(
                f"--rows {rows} × --columns {columns} = {product} panels, "
                f"but the data has {n}; they must match"
            )
        return rows, columns
    if columns is not None:
        if n % columns != 0:
            raise UsageError(
                f"--columns {columns} does not divide the data ({n} panels); "
                f"choose a count that divides {n}, or pass --rows so that "
                f"rows × columns equals {n}"
            )
        return n // columns, columns
    if n % rows != 0:
        raise UsageError(
            f"--rows {rows} does not divide the data ({n} panels); "
            f"choose a count that divides {n}, or pass --columns so that "
            f"rows × columns equals {n}"
        )
    return rows, n // rows


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


def _wind_component_role(da):
    """``'u'`` / ``'v'`` from CF ``standard_name``, or None."""
    sn = da.attrs.get("standard_name")
    if not isinstance(sn, str) or not sn.strip():
        return None
    key = sn.strip().lower()
    if "eastward" in key and "wind" in key:
        return "u"
    if "northward" in key and "wind" in key:
        return "v"
    return None


def _infer_uv_partner(name, *, want_v):
    """Guess the complementary u/v variable name, or None."""
    pairs = dict(_UV_NAME_PAIRS)
    inv = {v: u for u, v in _UV_NAME_PAIRS}
    if want_v:
        if name in pairs:
            return pairs[name]
        swapped = name.replace("eastward", "northward").replace("u_component", "v_component")
        if swapped != name:
            return swapped
        if name.startswith("u"):
            return "v" + name[1:]
        return None
    if name in inv:
        return inv[name]
    swapped = name.replace("northward", "eastward").replace("v_component", "u_component")
    if swapped != name:
        return swapped
    if name.startswith("v"):
        return "u" + name[1:]
    return None


def _resolve_uv(ds, u_variable, v_variable):
    """Eastward/northward variable names from flags, CF attrs, or common names."""
    names = list(ds.data_vars)
    if u_variable and u_variable not in ds:
        raise UsageError(f"--u-variable {u_variable!r} is not in the data (have {names})")
    if v_variable and v_variable not in ds:
        raise UsageError(f"--v-variable {v_variable!r} is not in the data (have {names})")
    if u_variable and v_variable:
        return u_variable, v_variable
    if u_variable:
        partner = _infer_uv_partner(u_variable, want_v=True)
        if partner and partner in ds:
            return u_variable, partner
        raise UsageError(
            f"--u-variable {u_variable!r} is set but no northward partner was found; "
            "pass --v-variable"
        )
    if v_variable:
        partner = _infer_uv_partner(v_variable, want_v=False)
        if partner and partner in ds:
            return partner, v_variable
        raise UsageError(
            f"--v-variable {v_variable!r} is set but no eastward partner was found; "
            "pass --u-variable"
        )
    u_cf, v_cf = [], []
    for name in names:
        role = _wind_component_role(ds[name])
        if role == "u":
            u_cf.append(name)
        elif role == "v":
            v_cf.append(name)
    if len(u_cf) == 1 and len(v_cf) == 1:
        return u_cf[0], v_cf[0]
    present = set(names)
    matches = [(u, v) for u, v in _UV_NAME_PAIRS if u in present and v in present]
    if matches:
        return matches[0]
    raise UsageError(
        "u/v plot needs eastward (u) and northward (v) wind components; "
        f"could not auto-detect them in {names}. Pass --u-variable and --v-variable."
    )


def _is_sample_dim(da, dim):
    """True if ``dim`` is flattened into wind-rose samples rather than indexed."""
    if dim in _SAMPLE_DIM_NAMES:
        return True
    for cf_name in ("latitude", "longitude", "time"):
        if cf_dim(da, cf_name) == dim:
            return True
    return False


def _flat_numeric(da):
    """Raveled float samples, stripping a pint wrapper if present."""
    import numpy as np

    if getattr(da.pint, "units", None) is not None:
        da = da.pint.dequantify()
    return np.asarray(da.values, dtype=float).reshape(-1)


def _uv_to_speed_fromdir(u, v):
    """Speed and meteorological FROM direction in degrees (0=N, 90=E)."""
    import numpy as np

    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    speed = np.hypot(u, v)
    fromdir = (np.degrees(np.arctan2(-u, -v)) + 360.0) % 360.0
    return speed, fromdir


def _speed_units_display(da):
    raw = variable_units(da)
    if not raw:
        return "m/s"
    if units_equal(raw, "m s-1"):
        return "m/s"
    return raw


def _speed_edges(speed, units):
    """Speed-bin edges. Standard 2 m/s classes when units are m/s, else 6 linear bins."""
    import numpy as np

    vmax = float(np.nanmax(speed)) if speed.size else 0.0
    ms = bool(units) and units_equal(units, "m s-1")
    if ms:
        return np.asarray([*WIND_SPEED_EDGES_MS, np.inf], dtype=float)
    if not np.isfinite(vmax) or vmax <= 0:
        return np.array([0.0, 1.0], dtype=float)
    return np.linspace(0.0, vmax, 7)


def _speed_bin_labels(edges):
    import numpy as np

    labels = []
    n = len(edges) - 1
    for i in range(n):
        lo = float(edges[i])
        hi = edges[i + 1]
        if np.isinf(hi):
            labels.append(f"≥{lo:g}")
        else:
            labels.append(f"{lo:g}–{hi:g}")
    return labels


def _speed_colors(n, colormap):
    import numpy as np
    from matplotlib import colormaps
    from matplotlib.colors import LinearSegmentedColormap

    if n < 1:
        return []
    if colormap is None:
        cmap = LinearSegmentedColormap.from_list("windrose", WIND_SPEED_COLORS)
    else:
        parsed = _parse_colormap(colormap)
        cmap = colormaps[parsed] if isinstance(parsed, str) else parsed
    if n == 1:
        return [cmap(0.5)]
    return [cmap(x) for x in np.linspace(0.0, 1.0, n)]


def _wind_rose_hist(speed, direction, speed_edges, nsector=WIND_ROSE_SECTORS):
    """2D histogram ``(nsector, nspeed)``. Sector 0 is North-centered."""
    import numpy as np

    offset = 180.0 / nsector
    shifted = (np.asarray(direction, dtype=float) + offset) % 360.0
    dir_edges = np.linspace(0.0, 360.0, nsector + 1)
    hist, _, _ = np.histogram2d(shifted, speed, bins=[dir_edges, speed_edges])
    return hist


def _windrose(speed, direction, *, title, fontsize, units_disp, colormap, units):
    """Polar stacked-bar wind rose; radial axis is frequency percent."""
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch

    speed_edges = _speed_edges(speed, units)
    hist = _wind_rose_hist(speed, direction, speed_edges)
    while hist.shape[1] > 1 and float(hist[:, -1].sum()) == 0:
        hist = hist[:, :-1]
        speed_edges = speed_edges[:-1]
    total = float(hist.sum())
    if total <= 0:
        raise UsageError("windrose has no finite u/v samples to plot.")
    freq = 100.0 * hist / total
    n_speed = freq.shape[1]
    colors = _speed_colors(n_speed, colormap)
    unit_suffix = f" {units_disp}" if units_disp else ""
    legend_labels = [f"{lab}{unit_suffix}" for lab in _speed_bin_labels(speed_edges)]

    nsector = WIND_ROSE_SECTORS
    width = 2.0 * np.pi / nsector
    theta = np.arange(nsector) * width
    fig = plt.figure(figsize=(8.5, 7.0))
    ax = fig.add_subplot(111, projection="polar")
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    bottom = np.zeros(nsector)
    for i in range(n_speed):
        ax.bar(
            theta,
            freq[:, i],
            width=width,
            bottom=bottom,
            color=colors[i],
            edgecolor="white",
            linewidth=0.4,
            align="center",
            zorder=2,
        )
        bottom += freq[:, i]
    ax.set_thetagrids(
        [0, 45, 90, 135, 180, 225, 270, 315],
        ["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
    )
    ax.tick_params(axis="y", labelsize=int(fontsize * 0.7))
    ax.set_ylim(0, max(float(bottom.max()) * 1.08, 1.0))
    ax.text(
        0.5,
        1.12,
        "Frequency (%)",
        transform=ax.transAxes,
        ha="center",
        fontsize=int(fontsize * 0.8),
    )
    handles = [
        Patch(facecolor=colors[i], edgecolor="white", label=legend_labels[i])
        for i in range(n_speed)
    ]
    ax.legend(
        handles=handles,
        title="Wind speed",
        loc="center left",
        bbox_to_anchor=(1.15, 0.5),
        fontsize=int(fontsize * 0.7),
        title_fontsize=int(fontsize * 0.75),
        frameon=False,
    )
    if title:
        fig.suptitle(title, fontsize=fontsize, y=0.98)
    return fig


def _plot_windrose(
    ds,
    u_variable,
    v_variable,
    variable,
    overrides,
    bbox_nwse,
    mask_geojson,
    title,
    fontsize,
    colormap,
):
    """Flatten u/v samples into one meteorological-from wind rose."""
    import numpy as np

    if variable:
        print(
            "Warning: --variable is ignored for --style windrose; "
            "use --u-variable/--v-variable or auto-detection.",
            file=sys.stderr,
        )
    u_name, v_name = _resolve_uv(ds, u_variable, v_variable)
    ds = to_standard_units(ds, variables=[u_name, v_name])
    u_da = ds[u_name]
    v_da = ds[v_name]
    u_da = _apply_index(u_da, overrides, list_dims=None)
    v_da = _apply_index(v_da, overrides, list_dims=None)
    extra = [d for d in u_da.dims if not _is_sample_dim(u_da, d)]
    if extra:
        raise UsageError(
            f"dimension {extra[0]!r} remains after selection; windrose "
            "flattens space/time/ensemble into samples — select a position "
            f"from {extra[0]!r} with --index"
        )
    region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
    if bbox_nwse is not None or region_polygon is not None:
        lat_dim = cf_dim(u_da, "latitude")
        lon_dim = cf_dim(u_da, "longitude")
        if lat_dim and lon_dim and lat_dim in u_da.dims and lon_dim in u_da.dims:
            u_da, _ = _subset_spatial(u_da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
            v_da, _ = _subset_spatial(v_da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
        else:
            u_da = _subset_points(u_da, bbox_nwse, region_polygon)
            v_da = _subset_points(v_da, bbox_nwse, region_polygon)
    u_vals = _flat_numeric(u_da)
    v_vals = _flat_numeric(v_da)
    if u_vals.size != v_vals.size:
        raise UsageError(
            f"u {u_name!r} and v {v_name!r} have different sizes after selection "
            f"({u_vals.size} vs {v_vals.size}); they must share coordinates"
        )
    valid = np.isfinite(u_vals) & np.isfinite(v_vals)
    u_vals, v_vals = u_vals[valid], v_vals[valid]
    if u_vals.size == 0:
        raise UsageError("windrose has no finite u/v samples to plot.")
    u_units = variable_units(u_da)
    v_units = variable_units(v_da)
    if u_units and v_units and not units_equal(u_units, v_units):
        raise UsageError(f"u units {u_units!r} do not match v units {v_units!r}")
    speed, direction = _uv_to_speed_fromdir(u_vals, v_vals)
    return _windrose(
        speed,
        direction,
        title=title,
        fontsize=fontsize,
        units_disp=_speed_units_display(u_da),
        colormap=colormap,
        units=u_units,
    )


def _wind_speed_da(u_da, v_da):
    """Speed from eastward/northward components, with a Wind speed label."""
    import numpy as np
    import xarray as xr

    u_da = _plain(u_da)
    v_da = _plain(v_da)
    speed = xr.apply_ufunc(np.hypot, u_da, v_da, keep_attrs=False)
    units = variable_units(u_da) or "m s-1"
    speed.name = "speed"
    speed.attrs.update(long_name="Wind speed", units=units, standard_name="wind_speed")
    return speed


def _wind_speed_cbar_label(u_da):
    units_disp = _speed_units_display(u_da)
    blob = " ".join(str(u_da.attrs.get(key) or "") for key in ("long_name", "GRIB_name")).lower()
    if "anomal" in blob:
        return f"Wind speed anomaly [{units_disp}]"
    return f"Wind speed [{units_disp}]"


def _quiver_map(
    speed,
    u_da,
    v_da,
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
    draw_boxes=None,
    rows=None,
    columns=None,
    quiver_scale=QUIVER_SCALE,
    cbar_label=None,
):
    """S2S-style speed pcolormesh with regridded u/v arrows (quiver_plot_variable)."""
    import cartopy.crs as ccrs
    import matplotlib.pyplot as plt
    import numpy as np

    def _prep(da):
        da = _plain(da)
        if "number" in da.dims:
            da = da.mean("number", keep_attrs=True)
        if wrap_lon:
            lon_vals = np.asarray(da[lon_dim].values)
            if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
                da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)
        return da

    speed = _prep(speed)
    u_da = _prep(u_da)
    v_da = _prep(v_da)

    sdim = _step_dim(speed)
    if sdim is None or speed.sizes.get(sdim, 1) == 1:
        if sdim and sdim in speed.dims:
            speed = speed.squeeze(sdim, drop=True)
            u_da = u_da.squeeze(sdim, drop=True)
            v_da = v_da.squeeze(sdim, drop=True)
        steps = [None]
        sdim = None
    else:
        steps = list(speed[sdim].values)

    title_steps = native_steps if native_steps is not None and native_step_dim == sdim else steps
    num_steps = len(steps)
    nrows, ncols = _panel_shape(num_steps, rows=rows, columns=columns)

    if extent is None:
        lat_vals = np.asarray(speed[lat_dim].values)
        lon_vals = np.asarray(speed[lon_dim].values)
        dlat = float(np.abs(np.diff(np.sort(lat_vals))).mean()) if lat_vals.size > 1 else 0.0
        dlon = float(np.abs(np.diff(np.sort(lon_vals))).mean()) if lon_vals.size > 1 else 0.0
        extent = [
            float(lon_vals.min()) - dlon / 2,
            float(lon_vals.max()) + dlon / 2,
            float(lat_vals.min()) - dlat / 2,
            float(lat_vals.max()) + dlat / 2,
        ]

    vmax = float(speed.max(skipna=True).values)
    vmin = float(speed.min(skipna=True).values)
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

    mesh = None
    quiv = None
    boxes = draw_boxes or []
    overlays = _load_geo_overlays(extent)
    for i, s in enumerate(steps):
        ax = axes[i]
        slab = speed if sdim is None else speed.isel({sdim: i})
        u_slab = u_da if sdim is None else u_da.isel({sdim: i})
        v_slab = v_da if sdim is None else v_da.isel({sdim: i})
        slab = slab.transpose(lat_dim, lon_dim)
        u_slab = u_slab.transpose(lat_dim, lon_dim)
        v_slab = v_slab.transpose(lat_dim, lon_dim)
        if wrap_lon:
            ax.set_extent(extent, crs=ccrs.PlateCarree())
        else:
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
        mesh = ax.pcolormesh(
            slab[lon_dim],
            slab[lat_dim],
            slab.values,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            transform=ccrs.PlateCarree(),
        )
        quiv = ax.quiver(
            u_slab[lon_dim],
            u_slab[lat_dim],
            u_slab.values,
            v_slab.values,
            transform=ccrs.PlateCarree(),
            regrid_shape=QUIVER_REGRID_SHAPE,
            scale=quiver_scale,
            color="k",
            zorder=5,
        )
        _draw_geo_overlays(ax, overlays, ccrs.PlateCarree())
        gl = ax.gridlines(draw_labels=True, alpha=0)
        gl.top_labels = False
        gl.right_labels = False
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        for city, (lat, lon) in cities.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=ccrs.PlateCarree())
            ax.text(lon - 2.0, lat + 0.5, city, fontsize=10, transform=ccrs.PlateCarree())
        if boxes:
            _draw_boxes_on_ax(ax, boxes, ccrs.PlateCarree())
        if s is not None:
            ax.set_title(_panel_title(speed, sdim, s, title_steps), fontsize=int(fontsize * 0.8))

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    last = axes[num_steps - 1]
    units_disp = _speed_units_display(u_da)
    y_key = 0.18
    for u_ref in QUIVER_KEY_MS:
        last.quiverkey(
            quiv,
            1.18,
            y_key,
            u_ref,
            f"{u_ref:g} {units_disp}",
            labelpos="E",
            coordinates="axes",
        )
        y_key -= 0.10

    if title:
        fig.suptitle(title, fontsize=fontsize)
    fig.tight_layout(rect=[0, 0, 1, 0.94] if title else None)
    cbar_ax = fig.add_axes([0.15, -0.04, 0.7, 0.01 + 0.02 / nrows])
    cbar = fig.colorbar(mesh, cax=cbar_ax, orientation="horizontal", fraction=5)
    cbar.set_label(cbar_label or _wind_speed_cbar_label(u_da), fontsize=fontsize)
    return fig


def _plot_quiver(
    ds,
    u_variable,
    v_variable,
    variable,
    overrides,
    bbox_nwse,
    mask_geojson,
    extent,
    cities,
    title,
    fontsize,
    colormap,
    draw_boxes,
    rows,
    columns,
    quiver_scale,
):
    """Map panels of wind speed with S2S-style u/v quiver overlay."""
    if variable:
        print(
            "Warning: --variable is ignored for --style quiver; "
            "use --u-variable/--v-variable or auto-detection.",
            file=sys.stderr,
        )
    u_name, v_name = _resolve_uv(ds, u_variable, v_variable)
    ds = to_standard_units(ds, variables=[u_name, v_name])
    u_da = ds[u_name]
    v_da = ds[v_name]
    u_units = variable_units(u_da)
    v_units = variable_units(v_da)
    if u_units and v_units and not units_equal(u_units, v_units):
        raise UsageError(f"u units {u_units!r} do not match v units {v_units!r}")
    u_da, lat_dim, lon_dim, extent_vals, wrap_lon, native_step_dim, native_steps = (
        _prepare_gridded_map(u_da, overrides, bbox_nwse, mask_geojson, extent, style="quiver")
    )
    v_da, *_ = _prepare_gridded_map(
        v_da, overrides, bbox_nwse, mask_geojson, extent, style="quiver"
    )
    speed = _wind_speed_da(u_da, v_da)
    cmap = _parse_colormap(colormap) if colormap else QUIVER_CMAP
    return _quiver_map(
        speed,
        u_da,
        v_da,
        lat_dim,
        lon_dim,
        cmap,
        extent_vals,
        _parse_cities(cities),
        title,
        fontsize,
        wrap_lon=wrap_lon,
        native_step_dim=native_step_dim,
        native_steps=native_steps,
        draw_boxes=draw_boxes,
        rows=rows,
        columns=columns,
        quiver_scale=quiver_scale if quiver_scale is not None else QUIVER_SCALE,
        cbar_label=_wind_speed_cbar_label(u_da),
    )


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
    draw_boxes=None,
    norm=None,
    flag_ticks=None,
    flag_labels=None,
    rows=None,
    columns=None,
):
    import cartopy.crs as ccrs
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm

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

    title_steps = native_steps if native_steps is not None and native_step_dim == sdim else steps

    num_steps = len(steps)
    nrows, ncols = _panel_shape(num_steps, rows=rows, columns=columns)

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
    if norm is not None:
        vmin, vmax = None, None

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
    boxes = draw_boxes or []
    overlays = _load_geo_overlays(extent)
    for i, s in enumerate(steps):
        ax = axes[i]
        slab = da if sdim is None else da.isel({sdim: i})
        slab = slab.transpose(lat_dim, lon_dim)
        contour = ax.pcolormesh(
            slab[lon_dim],
            slab[lat_dim],
            slab.values,
            cmap=cmap,
            norm=norm,
            vmin=vmin,
            vmax=vmax,
            transform=ccrs.PlateCarree(),
        )
        if wrap_lon:
            ax.set_extent(extent, crs=ccrs.PlateCarree())
        else:
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
        _draw_geo_overlays(ax, overlays, ccrs.PlateCarree())
        gl = ax.gridlines(draw_labels=True, alpha=0)
        gl.top_labels = False
        gl.right_labels = False
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        for city, (lat, lon) in cities.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=ccrs.PlateCarree())
            ax.text(lon - 2.0, lat + 0.5, city, fontsize=10, transform=ccrs.PlateCarree())
        if boxes:
            _draw_boxes_on_ax(ax, boxes, ccrs.PlateCarree())
        if s is not None:
            ax.set_title(_panel_title(da, sdim, s, title_steps), fontsize=int(fontsize * 0.8))

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    if title:
        fig.suptitle(title, fontsize=fontsize)
    fig.tight_layout(rect=[0, 0, 1, 0.94] if title else None)
    cbar_ax = fig.add_axes([0.15, -0.04, 0.7, 0.01 + 0.02 / nrows])
    cbar_kw = {}
    if isinstance(norm, BoundaryNorm):
        cbar_kw["spacing"] = "uniform"
        cbar_kw["ticks"] = list(norm.boundaries)
    cbar = fig.colorbar(contour, cax=cbar_ax, orientation="horizontal", fraction=5, **cbar_kw)
    if flag_ticks is not None:
        cbar.set_ticks(flag_ticks)
        if flag_labels is not None:
            cbar.set_ticklabels(flag_labels)
    cbar.set_label(_variable_label(da), fontsize=fontsize)
    return fig


@weather_skill(
    name="plot",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--style",
    choices=["heatmap", "timeseries", "windrose", "quiver"],
    default="heatmap",
)
@weather_skill.argument(
    "--u-variable",
    default=None,
    help="Eastward wind variable (windrose/quiver). Auto-detected when omitted.",
)
@weather_skill.argument(
    "--v-variable",
    default=None,
    help="Northward wind variable (windrose/quiver). Auto-detected when omitted.",
)
@weather_skill.argument(
    "--colormap",
    default=None,
    help=(
        "matplotlib colormap name, or comma-separated colors. "
        "Heatmap default: discrete Kenya/S2S precip classes for precip "
        "variables, else viridis. Windrose default: blue-to-orange speed classes. "
        "Quiver default: YlGn (ECMWF S2S 10 m / 700 hPa wind vectors)."
    ),
)
@weather_skill.argument(
    "--index",
    default=None,
    help=(
        "Slice like 'step=3,number=0' (heatmap, quiver, and windrose). "
        "Heatmap/quiver lists keep the dim as panels; windrose lists keep samples."
    ),
)
@weather_skill.argument(
    "--extent",
    default=None,
    help="Map extent 'lon_min,lon_max,lat_min,lat_max' (heatmap and quiver).",
)
@weather_skill.argument(
    "--cities",
    default=None,
    help='City overlay JSON (heatmap and quiver). Inline {"name": [lat, lon]} or file path.',
)
@weather_skill.argument("--fontsize", type=int, default=16)
@weather_skill.argument("--title", default=None, help="Optional plot title.")
@weather_skill.argument(
    "--rows",
    type=int,
    default=None,
    help=(
        "Heatmap/quiver panel rows. Alone or with --columns, the grid must pack the data exactly."
    ),
)
@weather_skill.argument(
    "--columns",
    type=int,
    default=None,
    help=(
        "Heatmap/quiver panel columns. Alone or with --rows, the grid must pack the data exactly."
    ),
)
@weather_skill.argument(
    "--mask-geojson",
    default=None,
    help="GeoJSON polygon; cells/points outside become NaN (heatmap, quiver, windrose).",
)
@weather_skill.argument(
    "--draw-box",
    action="append",
    default=None,
    help=(
        "Draw a black outline box on the map as N/W/S/E decimal degrees "
        "(same form as --bbox). Repeat for multiple boxes. Heatmap and quiver."
    ),
)
@weather_skill.argument(
    "--quiver-scale",
    type=float,
    default=None,
    help=(
        "Matplotlib quiver scale (S2S quiver_plot_variable default 40; "
        "their anomaly maps use 20). Quiver-only."
    ),
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
    draw_box,
    rows,
    columns,
    u_variable,
    v_variable,
    quiver_scale,
    output,
    **kwargs,
):
    """Render a heatmap, timeseries, wind-rose, or quiver PNG from a weather-skills standard dataset Zarr."""
    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter

    try:
        overrides = _parse_index(index)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    bbox_nwse = bbox
    draw_boxes = _parse_draw_boxes(draw_box)
    map_only = {
        "--extent": bool(extent),
        "--cities": bool(cities),
        "--draw-box": bool(draw_boxes),
        "--rows": rows is not None,
        "--columns": columns is not None,
    }
    spatial = {
        "--bbox": bbox_nwse is not None,
        "--mask-geojson": bool(mask_geojson),
        "--index": bool(overrides),
    }
    uv_flags = {
        "--u-variable": bool(u_variable),
        "--v-variable": bool(v_variable),
    }
    quiver_only = {
        "--quiver-scale": quiver_scale is not None,
    }

    def _flag_detail(flag):
        if flag == "--bbox" and bbox_nwse is not None:
            return f" {bbox_nwse[0]}/{bbox_nwse[1]}/{bbox_nwse[2]}/{bbox_nwse[3]}"
        if flag == "--extent":
            return f" {extent!r}"
        if flag == "--index":
            return f" {index!r}"
        if flag == "--draw-box":
            return f" {draw_box!r}"
        return ""

    if style == "timeseries":
        for flag, set_ in {**map_only, **spatial}.items():
            if set_:
                print(
                    f"Warning: {flag}{_flag_detail(flag)} is a heatmap-only option; "
                    f"ignored for --style {style}.",
                    file=sys.stderr,
                )
        for flag, set_ in {**uv_flags, **quiver_only}.items():
            if set_:
                print(
                    f"Warning: {flag} is ignored for --style {style}.",
                    file=sys.stderr,
                )
    elif style == "heatmap":
        for flag, set_ in {**uv_flags, **quiver_only}.items():
            if set_:
                print(
                    f"Warning: {flag} is only used with --style windrose or "
                    f"--style quiver; ignored for --style heatmap.",
                    file=sys.stderr,
                )
    elif style == "windrose":
        for flag, set_ in {**map_only, **quiver_only}.items():
            if set_:
                print(
                    f"Warning: {flag}{_flag_detail(flag)} is ignored for --style windrose.",
                    file=sys.stderr,
                )

    if style == "windrose":
        fig = _plot_windrose(
            ds,
            u_variable,
            v_variable,
            variable,
            overrides,
            bbox_nwse,
            mask_geojson,
            title,
            fontsize,
            colormap,
        )
    elif style == "quiver":
        fig = _plot_quiver(
            ds,
            u_variable,
            v_variable,
            variable,
            overrides,
            bbox_nwse,
            mask_geojson,
            extent,
            cities,
            title,
            fontsize,
            colormap,
            draw_boxes,
            rows,
            columns,
            quiver_scale,
        )
    else:
        variable = variable or auto_variable(ds)
        if not variable or variable not in ds:
            raise UsageError(f"no usable variable. Available: {list(ds.data_vars)}")
        ds = to_standard_units(ds, variables=[variable])
        ds = precip_for_display(ds, variable)
        da = ds[variable]

    if style == "heatmap":
        (
            da,
            lat_dim,
            lon_dim,
            extent_vals,
            wrap_lon,
            native_step_dim,
            native_steps,
        ) = _prepare_gridded_map(da, overrides, bbox_nwse, mask_geojson, extent, style="heatmap")
        cities_map = _parse_cities(cities)
        flag_scale = _discrete_flag_scale(da, colormap)
        if flag_scale is not None:
            cmap, norm, flag_ticks, flag_labels = flag_scale
        else:
            cmap, norm = _heatmap_scale(da, colormap)
            flag_ticks, flag_labels = None, None
        fig = _heatmap(
            da,
            lat_dim,
            lon_dim,
            cmap,
            extent_vals,
            cities_map,
            title,
            fontsize,
            wrap_lon=wrap_lon,
            native_step_dim=native_step_dim,
            native_steps=native_steps,
            draw_boxes=draw_boxes,
            norm=norm,
            flag_ticks=flag_ticks,
            flag_labels=flag_labels,
            rows=rows,
            columns=columns,
        )
    elif style == "timeseries":
        fig, ax = plt.subplots(figsize=(10, 6))
        sdim = "step" if "step" in da.dims else cf_dim(da, "time")
        if sdim is None:
            raise UsageError(f"timeseries needs 'step' or 'time'; got {list(da.dims)}.")
        reduce_dims = [d for d in da.dims if d != sdim]
        reduced = da.mean(reduce_dims, keep_attrs=True)
        xvals, xlabel = _timeseries_axis(reduced, sdim)
        ax.plot(xvals, reduced.values, marker="o", markersize=5)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(_variable_label(reduced))
        qty = variable_label_for_display(reduced, include_units=False)
        ax.set_title(title or f"{qty} ({style})")
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
