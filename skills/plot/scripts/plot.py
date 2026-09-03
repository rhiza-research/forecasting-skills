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
"""Render a heatmap, timeseries, xy scatter, wind-rose, or quiver PNG from a weather-skills standard dataset Zarr."""

import argparse
import json
import re
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.display_labels import dataset_display_label, resolve_input_labels
from weather_skills_core.standard_utils import (
    ensure_normalized_longitude,
    lat_slice,
    parse_bbox,
    polygon_from_geojson,
)
from weather_skills_core.units import (
    DATA_INTERVAL_ATTR,
    classify_variable,
    parse_aggregation_period,
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_label_for_display,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_INDEX_INT_RE = re.compile(r"[+-]?[0-9]+")

# CHIRPS-GEFS / Early Warning eXplorer rainfall-total classes (mm).
# Under (<2) is white; over (>2500) is pale pink.
PRECIP_COLORS = [
    "#ffffff",
    "#c7ffbb",
    "#75f676",
    "#1bb61d",
    "#b8edfb",
    "#50a5f8",
    "#1e6eec",
    "#dcdcff",
    "#a08bff",
    "#7060de",
    "#fff8ad",
    "#ff9d00",
    "#ff1400",
    "#a30005",
    "#e58d8b",
    "#ffe5e4",
]
PRECIP_BOUNDS = [2, 5, 10, 25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2500]
# Sub-pentad / daily totals (< 5 day aggregation): same colors, lower breaks.
PRECIP_SHORT_BOUNDS = [0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 75, 100, 150, 200]
# Use PRECIP_BOUNDS when aggregation_period is missing or ≥ this many days.
PRECIP_LONG_MIN_DAYS = 5

# CHIRPS-GEFS / Early Warning eXplorer rainfall-anomaly classes (mm).
# Under/over colors sit outside the labeled tick bounds.
PRECIP_ANOMALY_COLORS = [
    "#c00006",
    "#ff3300",
    "#ff9d00",
    "#ffe772",
    "#7a5044",
    "#b68c80",
    "#f2dcd1",
    "#ffffff",
    "#c7ffbb",
    "#75f676",
    "#1bb61c",
    "#9bd1f5",
    "#2583f5",
    "#dcdcff",
    "#8070ee",
]
PRECIP_ANOMALY_BOUNDS = [-500, -300, -200, -100, -50, -25, -10, 10, 25, 50, 100, 200, 300, 500]

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

# Speed field matches plot_s2s 10 m / 700 hPa (YlGn). Arrows sit on the
# native grid (plot_wind_and_sst_anomaly), thinned to ~1.5°. Scale is
# auto-picked so a typical wind is ~1.5× that spacing — a fixed 100 matches
# S2S *anomaly* magnitudes and overdraws 10 m/s basin winds.
QUIVER_CMAP = "YlGn"
QUIVER_SCALE = 100.0
QUIVER_STEP = 1
QUIVER_TARGET_SPACING_DEG = 1.5
QUIVER_ARROW_LEN_SPACING = 1.5
QUIVER_KEY_MS = (5.0, 10.0)

# Natural Earth scale vs map span (max of lon/lat extent in degrees).
# Admin-1 (states / provinces / counties) is only readable on country-scale
# views; a multi-country or basin map would be a thicket of province lines.
_ADMIN1_MAX_SPAN_DEG = 20.0
_HIRES_MAX_SPAN_DEG = 45.0
_MIDRES_MAX_SPAN_DEG = 90.0

_ADMIN1_STYLE = {"facecolor": "none", "edgecolor": "0.45", "linewidth": 0.4, "zorder": 3}
_LAKES_STYLE = {"facecolor": "none", "edgecolor": "0.2", "linewidth": 0.5, "zorder": 3.5}
_BORDERS_STYLE = {"facecolor": "none", "edgecolor": "0.15", "linewidth": 0.8, "zorder": 4}
_COAST_STYLE = {"facecolor": "none", "edgecolor": "black", "linewidth": 0.8, "zorder": 4}

_LAYER_KINDS = frozenset({"heatmap", "scatter", "quiver", "outline", "mask"})
_ZARR_LAYER_KINDS = frozenset({"heatmap", "scatter", "quiver"})
_LAYER_OPTION_KEYS = frozenset(
    {
        "variable",
        "colormap",
        "index",
        "u-variable",
        "v-variable",
        "quiver-scale",
        "quiver-step",
    }
)
_KIND_ZORDER = {"heatmap": 1.0, "quiver": 5.0, "scatter": 6.0, "outline": 7.0}


class LayerSpec:
    """One ``--layer KIND:PATH[::k=v]`` entry. The decorator may set ``.ds``."""

    def __init__(self, kind, path, options, raw):
        self.kind = kind
        self.path = Path(path)
        self.options = options
        self.raw = raw
        self.ds = None

    def zarr_paths(self):
        if self.kind in _ZARR_LAYER_KINDS:
            return [self.path]
        return []

    def __str__(self):
        return self.raw

    def __repr__(self):
        return f"LayerSpec({self.raw!r})"


def _parse_layer_options(blob):
    """Parse ``k=v,k=v``; tokens without ``=`` continue the previous value (for ``index=step=0,1,2``)."""
    options = {}
    current = None
    for token in blob.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            key, _, val = token.partition("=")
            key = key.strip()
            if not key:
                raise ValueError(f"--layer option {token!r} has an empty key")
            if key not in _LAYER_OPTION_KEYS:
                raise ValueError(
                    f"unknown --layer option {key!r}; "
                    f"expected one of {', '.join(sorted(_LAYER_OPTION_KEYS))}"
                )
            if key in options:
                raise ValueError(f"--layer option {key!r} is given more than once")
            current = key
            options[key] = val.strip()
        else:
            if current is None:
                raise ValueError(f"--layer option {token!r} appears before any key=value")
            options[current] = f"{options[current]},{token}"
    return options


def parse_layer(value):
    """Argparse converter for ``KIND:PATH`` or ``KIND:PATH::k=v[,k=v...]``."""
    if not value or not str(value).strip():
        raise argparse.ArgumentTypeError("--layer spec is empty")
    raw = str(value).strip()
    if "::" in raw:
        head, _, opt_blob = raw.partition("::")
    else:
        head, opt_blob = raw, ""
    if ":" not in head:
        raise argparse.ArgumentTypeError(
            f"--layer {raw!r} must be KIND:PATH (e.g. heatmap:/tmp/a.zarr)"
        )
    kind, _, path = head.partition(":")
    kind = kind.strip().lower()
    path = path.strip()
    if kind not in _LAYER_KINDS:
        raise argparse.ArgumentTypeError(
            f"unknown --layer kind {kind!r}; expected one of {', '.join(sorted(_LAYER_KINDS))}"
        )
    if not path:
        raise argparse.ArgumentTypeError(f"--layer {raw!r} is missing a path")
    try:
        options = _parse_layer_options(opt_blob) if opt_blob else {}
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return LayerSpec(kind, path, options, raw)


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

    da = ensure_normalized_longitude(da, lon_dim)
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


def _prepare_gridded_map(
    da, overrides, bbox_nwse, mask_geojson, extent, *, style, region_polygon=None
):
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
    if region_polygon is None and mask_geojson:
        region_polygon = polygon_from_geojson(mask_geojson)
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


def _is_precip_anomaly(da):
    """True when precip looks like an anomaly (negatives or 'anomal' in name)."""
    import numpy as np

    name = f"{da.name or ''} {da.attrs.get('long_name', '')}".lower()
    if "anomal" in name:
        return True
    try:
        vmin = float(np.nanmin(np.asarray(da.values, dtype=float)))
    except (TypeError, ValueError):
        return False
    return np.isfinite(vmin) and vmin < 0


def _aggregation_days(da):
    """Return stamped ``aggregation_period`` in days, or None."""
    period = da.attrs.get("aggregation_period")
    if not (isinstance(period, str) and period.strip()):
        return None
    try:
        return float(parse_aggregation_period(period).to("day").magnitude)
    except UsageError:
        return None


def _precip_scale(da=None):
    """Discrete CHIRPS-GEFS rainfall-total classes with under/over colors.

    Periods shorter than ``PRECIP_LONG_MIN_DAYS`` use ``PRECIP_SHORT_BOUNDS``;
    longer (or unknown) periods use the dekadal-style ``PRECIP_BOUNDS``.
    """
    from matplotlib.colors import BoundaryNorm, ListedColormap

    days = _aggregation_days(da) if da is not None else None
    short = days is not None and days < PRECIP_LONG_MIN_DAYS
    colors = PRECIP_COLORS
    bounds = PRECIP_SHORT_BOUNDS if short else PRECIP_BOUNDS
    name = "chirps_short" if short else "chirps_total"
    cmap = ListedColormap(colors[1:-1], name=name)
    cmap.set_under(colors[0])
    cmap.set_over(colors[-1])
    return cmap, BoundaryNorm(bounds, ncolors=cmap.N, clip=False)


def _precip_anomaly_scale():
    """Discrete CHIRPS-GEFS rainfall-anomaly classes with under/over colors."""
    from matplotlib.colors import BoundaryNorm, ListedColormap

    colors = PRECIP_ANOMALY_COLORS
    cmap = ListedColormap(colors[1:-1], name="chirps_anom")
    cmap.set_under(colors[0])
    cmap.set_over(colors[-1])
    return cmap, BoundaryNorm(PRECIP_ANOMALY_BOUNDS, ncolors=cmap.N, clip=False)


def _heatmap_scale(da, colormap):
    """Return ``(cmap, norm)``. ``norm`` is set for the default precip scale."""
    if colormap:
        return _parse_colormap(colormap), None
    if _is_precip(da):
        if _is_precip_anomaly(da):
            return _precip_anomaly_scale()
        return _precip_scale(da)
    return "viridis", None


def _cbar_boundary_kwargs(norm, cmap=None):
    """Colorbar kwargs for a BoundaryNorm scale (ticks, spacing, optional extend)."""
    from matplotlib.colors import BoundaryNorm

    if not isinstance(norm, BoundaryNorm):
        return {}
    kw = {"spacing": "uniform", "ticks": list(norm.boundaries)}
    if getattr(cmap, "name", None) in ("chirps_anom", "chirps_total", "chirps_short"):
        kw["extend"] = "both"
    return kw


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
    if span > _MIDRES_MAX_SPAN_DEG:
        return {"scale": "110m", "admin1": False}
    if span > _HIRES_MAX_SPAN_DEG:
        return {"scale": "50m", "admin1": False}
    return {"scale": "10m", "admin1": span <= _ADMIN1_MAX_SPAN_DEG}


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


def _axis_label(text):
    """Sentence-case an axis label; map lon/lat shorthand to Longitude/Latitude."""
    if text is None:
        return text
    s = str(text).strip()
    if not s:
        return s
    known = {
        "lon": "Longitude",
        "lat": "Latitude",
        "longitude": "Longitude",
        "latitude": "Latitude",
        "valid time": "Valid time",
        "calendar day": "Calendar day",
        "time": "Time",
        "step": "Step",
        "forecast step": "Forecast step",
    }
    key = s.lower()
    if key in known:
        return known[key]
    if s[:1].islower():
        return s[:1].upper() + s[1:]
    return s


def _resolve_axis_label(override, default):
    """Use ``override`` verbatim when set; otherwise sentence-case ``default``."""
    if override is not None and str(override).strip() != "":
        return str(override)
    return _axis_label(default)


def _format_step(value):
    import numpy as np

    arr = np.asarray(value)
    if arr.dtype.kind == "M":
        return np.datetime_as_string(arr.astype("datetime64[D]"), unit="D")
    if arr.dtype.kind == "m":
        days = arr.astype("timedelta64[D]").astype(int)
        return f"+{days}d"
    return str(value)


def _calendar_bin_width(da, all_steps):
    """Timedelta for a left-labeled multi-day calendar bin, or None for a single date."""
    import numpy as np
    import pandas as pd

    days = _aggregation_days(da)
    if days is not None and days >= 2:
        return pd.Timedelta(days=float(days))
    arr = np.asarray(all_steps)
    if arr.size < 2 or arr.dtype.kind != "M":
        return None
    try:
        diffs = np.diff(arr.astype("datetime64[ns]").astype("int64"))
    except (TypeError, ValueError):
        return None
    positive = diffs[diffs > 0]
    if positive.size == 0:
        return None
    median_ns = float(np.median(positive))
    if median_ns < 2 * 86_400_000_000_000:  # < 2 days → single-date label
        return None
    return pd.Timedelta(median_ns, unit="ns")


def _format_calendar_panel(value, bin_width=None):
    """``YYYY-MM-DD``, or ``YYYY-MM-DD to YYYY-MM-DD`` for multi-day left-edge bins."""
    import datetime as _dt

    import numpy as np
    import pandas as pd

    if hasattr(value, "calendar"):
        if bin_width is None:
            return value.strftime("%Y-%m-%d")
        try:
            end = value + bin_width - _dt.timedelta(days=1)
        except (TypeError, ValueError):
            return value.strftime("%Y-%m-%d")
        return f"{value.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"

    try:
        start = pd.Timestamp(np.asarray(value).item() if hasattr(value, "dtype") else value)
    except (TypeError, ValueError):
        return _format_step(value)
    if bin_width is None:
        return start.date().isoformat()
    try:
        end = start + bin_width - pd.Timedelta(days=1)
    except (TypeError, ValueError):
        return start.date().isoformat()
    return f"{start.date().isoformat()} to {end.date().isoformat()}"


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
    return (init + steps).astype("datetime64[ns]"), "Valid time"


def _calendar_year(value) -> int:
    """Calendar year from a datetime-like sample (numpy, cftime, or datetime)."""
    import numpy as np

    if hasattr(value, "year"):
        return int(value.year)
    arr = np.asarray(value)
    if arr.dtype.kind == "M":
        return int(arr.astype("datetime64[Y]").astype(int) + 1970)
    raise UsageError(f"--pair-on year needs datetime samples; got {value!r}")


def _pair_key(value, pair_on: str):
    """Hashable alignment key for one sample along ``--pair-on``."""
    import numpy as np

    if pair_on == "year":
        return _calendar_year(value)
    arr = np.asarray(value)
    if arr.dtype.kind == "M":
        return str(np.datetime64(arr, "D"))
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return value


def _xy_1d(ds, variable, overrides, bbox_nwse, region_polygon, role: str):
    """Reduce one input to a 1D series plus pairing-axis values."""
    import numpy as np

    variable = variable or auto_variable(ds)
    if not variable or variable not in ds:
        raise UsageError(
            f"{role} has no usable variable {variable!r}. Available: {list(ds.data_vars)}"
        )
    try:
        ds = to_standard_units(ds, variables=[variable])
    except UsageError:
        # Totals (mm) or anomalies can still carry a rate/temp standard_name
        # that classify_variable would force into an incompatible target.
        pass
    ds = precip_for_display(ds, variable)
    da = _apply_index(_plain(ds[variable]), overrides, list_dims=())
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if (bbox_nwse is not None or region_polygon is not None) and lat_dim and lon_dim:
        if lat_dim in da.dims and lon_dim in da.dims:
            da, _ = _subset_spatial(da, lat_dim, lon_dim, bbox_nwse, region_polygon, None)
    sdim = "step" if "step" in da.dims else cf_dim(da, "time")
    if sdim is None:
        if da.ndim == 1:
            sdim = da.dims[0]
        else:
            raise UsageError(
                f"{role} needs a time/step axis to pair samples; got {list(da.dims)}."
            )
    reduce_dims = [d for d in da.dims if d != sdim]
    reduced = da.mean(reduce_dims, keep_attrs=True) if reduce_dims else da
    axis_vals, _ = _timeseries_axis(reduced, sdim)
    values = np.asarray(_plain(reduced).values, dtype=float)
    return reduced, np.asarray(axis_vals), values


def _pair_xy(x_axis, x_vals, y_axis, y_vals, pair_on: str):
    """Inner-join two 1D series on time, calendar year, or position."""
    import numpy as np

    x_vals = np.asarray(x_vals, dtype=float)
    y_vals = np.asarray(y_vals, dtype=float)
    if pair_on == "index":
        if x_vals.size != y_vals.size:
            raise UsageError(
                f"--pair-on index needs the same number of samples "
                f"(--x has {x_vals.size}, --y has {y_vals.size})."
            )
        keys = list(range(x_vals.size))
        return x_vals, y_vals, keys

    x_keys = [_pair_key(v, pair_on) for v in np.ravel(x_axis)]
    y_keys = [_pair_key(v, pair_on) for v in np.ravel(y_axis)]
    x_map: dict = {}
    for i, key in enumerate(x_keys):
        if key in x_map:
            raise UsageError(
                f"--pair-on {pair_on} has duplicate {key!r} on --x; "
                "aggregate or select so each key appears once."
            )
        x_map[key] = i
    y_map: dict = {}
    for i, key in enumerate(y_keys):
        if key in y_map:
            raise UsageError(
                f"--pair-on {pair_on} has duplicate {key!r} on --y; "
                "aggregate or select so each key appears once."
            )
        y_map[key] = i
    shared = [key for key in x_keys if key in y_map]
    if not shared:
        raise UsageError(
            f"--pair-on {pair_on} found no matching samples between --x and --y."
        )
    x_out = np.array([x_vals[x_map[k]] for k in shared], dtype=float)
    y_out = np.array([y_vals[y_map[k]] for k in shared], dtype=float)
    return x_out, y_out, shared


def _plot_xy(
    x_ds,
    y_ds,
    x_variable,
    y_variable,
    pair_on,
    overrides,
    bbox_nwse,
    mask_geojson,
    title,
    xlabel,
    ylabel,
    fontsize,
):
    """Scatter --x against --y after reducing each input to 1D and pairing samples."""
    import matplotlib.pyplot as plt
    import numpy as np

    region_polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
    x_da, x_axis, x_raw = _xy_1d(
        x_ds, x_variable, overrides, bbox_nwse, region_polygon, "--x"
    )
    y_da, y_axis, y_raw = _xy_1d(
        y_ds, y_variable, overrides, bbox_nwse, region_polygon, "--y"
    )
    x_vals, y_vals, keys = _pair_xy(x_axis, x_raw, y_axis, y_raw, pair_on)
    finite = np.isfinite(x_vals) & np.isfinite(y_vals)
    x_vals, y_vals = x_vals[finite], y_vals[finite]
    keys = [k for k, keep in zip(keys, finite, strict=True) if keep]
    if x_vals.size == 0:
        raise UsageError("xy scatter has no finite paired samples to plot.")

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.scatter(x_vals, y_vals, s=36, zorder=3)
    if pair_on == "year" or (pair_on == "time" and x_vals.size <= 25):
        for xv, yv, key in zip(x_vals, y_vals, keys, strict=True):
            ax.annotate(
                str(key),
                (xv, yv),
                textcoords="offset points",
                xytext=(4, 4),
                fontsize=max(8, int(round(fontsize * 0.55))),
            )
    tick_fs = max(10, int(round(fontsize * 0.7)))
    ax.set_xlabel(_resolve_axis_label(xlabel, _variable_label(x_da)), fontsize=fontsize)
    ax.set_ylabel(_resolve_axis_label(ylabel, _variable_label(y_da)), fontsize=fontsize)
    x_qty = variable_label_for_display(x_da, include_units=False)
    y_qty = variable_label_for_display(y_da, include_units=False)
    ax.set_title(title or f"{y_qty} vs {x_qty}", fontsize=fontsize)
    ax.tick_params(labelsize=tick_fs)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig


def _panel_title(da, sdim, step_value, all_steps):
    """Human panel label: calendar range, forecast valid window, or ``<sdim>=…``."""
    import numpy as np

    step_arr = np.asarray(all_steps)
    value_arr = np.asarray(step_value)

    # Forecast ``step`` (timedelta) + scalar init ``time`` → valid-time window.
    if (
        step_arr.dtype.kind == "m"
        and "time" in da.coords
        and getattr(da["time"], "ndim", 1) == 0
    ):
        fallback = f"{sdim}={_format_step(step_value)}"
        try:
            time_val = np.asarray(da["time"].values)
            start = time_val + np.asarray(step_value)
            dt = None
            interval = da.attrs.get(DATA_INTERVAL_ATTR)
            if isinstance(interval, str) and interval.strip():
                try:
                    seconds = float(parse_aggregation_period(interval).to("second").magnitude)
                    dt = np.timedelta64(int(round(seconds)), "s")
                except (TypeError, ValueError, UsageError):
                    dt = None
            if dt is None:
                dt = step_arr[1] - step_arr[0] if step_arr.size > 1 else np.timedelta64(1, "D")
            end = start + dt
            return f"{str(start)[:16]} until {str(end)[:16]}"
        except Exception:  # noqa: BLE001
            return fallback

    # Calendar ``time`` (or similar) panels — prefer date / date-range labels.
    if value_arr.dtype.kind == "M" or hasattr(step_value, "calendar"):
        return _format_calendar_panel(step_value, _calendar_bin_width(da, all_steps))

    return f"{sdim}={_format_step(step_value)}"


def _panel_title_fontsize(fontsize):
    """Panel date labels track ``--fontsize`` (not a reduced fraction)."""
    return int(fontsize)


def _map_colorbar_axes(fig, *, title, nrows, index=0, n_cbars=1):
    """Axes for a horizontal colorbar with clear gap under the map row(s)."""
    top = 0.92 if title else 0.98
    # Leave room for lon tick labels, a gap, then one or more thicker colorbars.
    stack_step = 0.07
    bottom = 0.12 + stack_step * max(0, n_cbars - 1)
    if index == 0:
        fig.tight_layout(rect=[0, bottom, 1, top])
    height = 0.04
    y = 0.03 + index * stack_step
    return fig.add_axes([0.12, y, 0.76, height])


def _colorbar_text_sizes(fontsize):
    """Colorbar text is intentionally smaller than axis/title text."""
    label_fs = max(9, int(round(fontsize * 0.72)))
    tick_fs = max(8, int(round(fontsize * 0.55)))
    return label_fs, tick_fs


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


def _mean_axis_spacing(values, axis):
    """Mean absolute spacing along one axis of a 1-D or 2-D coordinate."""
    import numpy as np

    values = np.asarray(values, dtype=float)
    if values.ndim == 0 or values.shape[axis] < 2:
        return None
    delta = np.diff(values, axis=axis)
    delta = delta[np.isfinite(delta)]
    if delta.size == 0:
        return None
    return float(np.mean(np.abs(delta)))


def _native_spacing_deg(lat, lon):
    """Finest mean lat/lon spacing in degrees, or None if it cannot be measured."""
    import numpy as np

    lat = np.asarray(lat)
    lon = np.asarray(lon)
    if lat.ndim == 1 and lon.ndim == 1:
        spacings = [_mean_axis_spacing(lat, 0), _mean_axis_spacing(lon, 0)]
    else:
        spacings = [
            _mean_axis_spacing(lat, 0),
            _mean_axis_spacing(lon, 1 if lon.ndim > 1 else 0),
        ]
    candidates = [s for s in spacings if s is not None and s > 0]
    return min(candidates) if candidates else None


def _quiver_step(lat, lon, requested=None, target_spacing=QUIVER_TARGET_SPACING_DEG):
    """Stride for quiver arrows.

    ``plot_wind_and_sst_anomaly`` uses ``quiver_step=1`` on the native S2S
    ~1.5° grid. When ``requested`` is set, use that. Otherwise thin finer
    grids (GFS 0.25°, ERA5) to about 1.5° so basin maps match that look.
    """
    if requested is not None:
        if requested < 1:
            raise UsageError("--quiver-step must be >= 1")
        return int(requested)
    spacing = _native_spacing_deg(lat, lon)
    if spacing is None:
        return QUIVER_STEP
    return max(QUIVER_STEP, int(round(target_spacing / spacing)))


def _auto_quiver_scale(u, v, lon_span, spacing_deg, requested=None):
    """Matplotlib quiver ``scale`` (data units per axes-width).

    Larger scale → shorter arrows. ``requested`` (``--quiver-scale``) wins.
    Otherwise size a typical (95th-percentile) wind to about
    ``QUIVER_ARROW_LEN_SPACING`` times the subsampled grid spacing, as a
    fraction of the map width, so 10 m/s basin winds and small anomalies
    both stay readable.
    """
    if requested is not None:
        if requested <= 0:
            raise UsageError("--quiver-scale must be > 0")
        return float(requested)
    import numpy as np

    speed = np.hypot(np.asarray(u, dtype=float), np.asarray(v, dtype=float))
    speed = speed[np.isfinite(speed)]
    if speed.size == 0 or lon_span <= 0 or spacing_deg is None or spacing_deg <= 0:
        return QUIVER_SCALE
    typical = float(np.percentile(speed, 95))
    if typical <= 0:
        return QUIVER_SCALE
    target_deg = QUIVER_ARROW_LEN_SPACING * float(spacing_deg)
    return typical * float(lon_span) / target_deg


def _subsample_quiver(lon, lat, u, v, step):
    """Native-grid u/v subsample, matching plot_wind_and_sst_anomaly."""
    import numpy as np

    lon = np.asarray(lon)
    lat = np.asarray(lat)
    u = np.asarray(u)
    v = np.asarray(v)
    if lon.ndim == 1 and lat.ndim == 1:
        lon, lat = np.meshgrid(lon, lat)
    step = max(1, int(step))
    return lon[::step, ::step], lat[::step, ::step], u[::step, ::step], v[::step, ::step]


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
    quiver_scale=None,
    quiver_step=None,
    cbar_label=None,
    xlabel=None,
    ylabel=None,
):
    """Speed pcolormesh with native-grid u/v arrows (plot_wind_and_sst_anomaly)."""
    import cartopy.crs as ccrs
    import matplotlib.pyplot as plt
    import numpy as np

    def _prep(da):
        da = _plain(da)
        if "number" in da.dims:
            da = da.mean("number", keep_attrs=True)
        if wrap_lon:
            da = ensure_normalized_longitude(da, lon_dim)
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
        extent = _extent_from_field(speed, lat_dim, lon_dim)

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
    step = _quiver_step(speed[lat_dim].values, speed[lon_dim].values, quiver_step)
    native_spacing = _native_spacing_deg(speed[lat_dim].values, speed[lon_dim].values)
    arrow_spacing = None if native_spacing is None else native_spacing * step
    lon_span = abs(extent[1] - extent[0])
    scale = _auto_quiver_scale(
        u_da.values, v_da.values, lon_span, arrow_spacing, requested=quiver_scale
    )
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
        lon_q, lat_q, u_q, v_q = _subsample_quiver(
            u_slab[lon_dim].values,
            u_slab[lat_dim].values,
            u_slab.values,
            v_slab.values,
            step,
        )
        quiv = ax.quiver(
            lon_q,
            lat_q,
            u_q,
            v_q,
            transform=ccrs.PlateCarree(),
            scale=scale,
            color="k",
            zorder=5,
        )
        _draw_geo_overlays(ax, overlays, ccrs.PlateCarree())
        gl = ax.gridlines(draw_labels=True, alpha=0)
        gl.top_labels = False
        gl.right_labels = False
        ax.set_xlabel(_resolve_axis_label(xlabel, "Longitude"))
        ax.set_ylabel(_resolve_axis_label(ylabel, "Latitude"))
        for city, (lat, lon) in cities.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=ccrs.PlateCarree())
            ax.text(
                lon - 2.0,
                lat + 0.5,
                city,
                fontsize=max(10, int(round(fontsize * 0.65))),
                transform=ccrs.PlateCarree(),
            )
        if boxes:
            _draw_boxes_on_ax(ax, boxes, ccrs.PlateCarree())
        if s is not None:
            ax.set_title(
                _panel_title(speed, sdim, s, title_steps),
                fontsize=_panel_title_fontsize(fontsize),
            )

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
    cbar_ax = _map_colorbar_axes(fig, title=title, nrows=nrows)
    cbar = fig.colorbar(mesh, cax=cbar_ax, orientation="horizontal", fraction=5)
    cbar_label_fs, cbar_tick_fs = _colorbar_text_sizes(fontsize)
    cbar.set_label(cbar_label or _wind_speed_cbar_label(u_da), fontsize=cbar_label_fs)
    cbar.ax.tick_params(labelsize=cbar_tick_fs)
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
    quiver_step,
    xlabel=None,
    ylabel=None,
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
        quiver_scale=quiver_scale,
        quiver_step=quiver_step,
        cbar_label=_wind_speed_cbar_label(u_da),
        xlabel=xlabel,
        ylabel=ylabel,
    )


def _is_cftime_axis(values):
    import numpy as np

    return (
        getattr(values.dtype, "kind", None) == "O"
        and values.size > 0
        and hasattr(np.asarray(values).flat[0], "calendar")
    )


def _axis_kind(values):
    kind = getattr(values.dtype, "kind", None)
    if kind == "M":
        return "datetime"
    if kind == "m":
        return "timedelta"
    if _is_cftime_axis(values):
        return "datetime"
    return None


def _label_key(value):
    import numpy as np

    arr = np.asarray(value)
    if arr.dtype.kind in ("M", "m"):
        return int(arr.astype("int64"))
    obj = arr.item() if getattr(arr, "shape", ()) == () else value
    if hasattr(obj, "calendar"):
        return (obj.calendar, str(obj))
    return obj


def _point_dim(ds):
    for name in ("station_id", "point_id"):
        if name in ds.dims:
            return name
    return None


def _combined_mask_polygon(mask_geojson, layers):
    paths = []
    flags = []
    if mask_geojson:
        paths.append(mask_geojson)
        flags.append("--mask-geojson")
    for spec in layers:
        if spec.kind == "mask":
            paths.append(spec.path)
            flags.append("--layer mask")
    if not paths:
        return None
    from shapely.ops import unary_union

    geoms = [polygon_from_geojson(path, flag=flag) for path, flag in zip(paths, flags, strict=True)]
    return geoms[0] if len(geoms) == 1 else unary_union(geoms)


def _layer_overrides(spec, default_index):
    raw = spec.options.get("index", default_index)
    if not raw:
        return {}
    try:
        return _parse_index(raw)
    except ValueError as exc:
        raise UsageError(f"--layer {spec.kind}:{spec.path}: {exc}") from None


def _copy_layer(spec, options=None):
    out = LayerSpec(
        spec.kind, spec.path, options if options is not None else spec.options, spec.raw
    )
    out.ds = spec.ds
    return out


def _ensure_layer_dataset(spec):
    if spec.kind not in _ZARR_LAYER_KINDS:
        return
    if spec.ds is not None:
        return
    import xarray as xr

    if not spec.path.exists():
        raise UsageError(f"input not found: {spec.path}")
    spec.ds = xr.open_zarr(spec.path, consolidated=True)


def _layer_variable(ds, spec):
    variable = spec.options.get("variable") or auto_variable(ds)
    if not variable or variable not in ds:
        raise UsageError(
            f"--layer {spec.kind}:{spec.path}: no usable variable. Available: {list(ds.data_vars)}"
        )
    return variable


def _common_labels(driver_values, other_values, spec):
    other_keys = {_label_key(v) for v in other_values}
    common = [v for v in driver_values if _label_key(v) in other_keys]
    if not common:
        raise UsageError(
            f"no overlapping time bins between the panel axis and --layer {spec.kind}:{spec.path}; "
            "aggregate both inputs to a common resolution first, e.g. with the "
            "aggregate-temporal skill"
        )
    return common


def _align_panel_labels(driver_dim, driver_values, driver_kind, da, spec):
    """Return ``(panel_dim or None, labels or None)`` for this layer vs the driver."""
    other_dim = _step_dim(da)
    if other_dim is None:
        return None, None
    other_values = list(da[other_dim].values)
    other_kind = _axis_kind(da[other_dim].values)
    if driver_kind != other_kind or driver_kind is None or other_kind is None:
        driver_name = "forecast step" if driver_kind == "timedelta" else "calendar time"
        other_name = "forecast step" if other_kind == "timedelta" else "calendar time"
        if driver_kind == "timedelta" or other_kind == "timedelta":
            raise UsageError(
                f"--layer {spec.kind}:{spec.path} has a {other_name} axis ({other_dim!r}) but the "
                f"panel axis is a {driver_name} axis ({driver_dim!r}). Run the step-to-time skill "
                "on the forecast before overlaying observations."
            )
        raise UsageError(
            f"--layer {spec.kind}:{spec.path} time axis {other_dim!r} is not comparable to "
            f"panel axis {driver_dim!r}"
        )
    return other_dim, _common_labels(driver_values, other_values, spec)


def _pad_cell_extent(lat_vals, lon_vals):
    """Map extent from cell centers, padded by half a grid step.

    A global 0–360 (or −180…180−ε) longitude axis pads to a 360° span whose
    endpoints are the same meridian. Cartopy ``set_extent`` then collapses the
    map to a ~9° sliver at the antimeridian instead of a basin/world view.
    """
    import numpy as np

    lat_vals = np.asarray(lat_vals)
    lon_vals = np.asarray(lon_vals)
    dlat = float(np.abs(np.diff(np.sort(lat_vals))).mean()) if lat_vals.size > 1 else 0.0
    dlon = float(np.abs(np.diff(np.sort(lon_vals))).mean()) if lon_vals.size > 1 else 0.0
    lon_min = float(np.nanmin(lon_vals)) - dlon / 2
    lon_max = float(np.nanmax(lon_vals)) + dlon / 2
    lat_min = float(np.nanmin(lat_vals)) - dlat / 2
    lat_max = float(np.nanmax(lat_vals)) + dlat / 2
    if lon_max - lon_min >= 360.0 - 1e-6:
        lon_min, lon_max = -180.0, 180.0
    lat_min = max(lat_min, -90.0)
    lat_max = min(lat_max, 90.0)
    return [lon_min, lon_max, lat_min, lat_max]


def _extent_from_field(da, lat_dim, lon_dim):
    return _pad_cell_extent(da[lat_dim].values, da[lon_dim].values)


def _extent_from_points(da):
    import numpy as np

    lat_name = cf_dim(da, "latitude")
    lon_name = cf_dim(da, "longitude")
    lats = np.asarray(da[lat_name].values, dtype=float)
    lons = np.asarray(da[lon_name].values, dtype=float)
    lats = lats[np.isfinite(lats)]
    lons = lons[np.isfinite(lons)]
    if lats.size == 0 or lons.size == 0:
        raise UsageError("scatter layer has no finite lat/lon coordinates")
    pad = 0.5
    return [
        float(lons.min()) - pad,
        float(lons.max()) + pad,
        float(lats.min()) - pad,
        float(lats.max()) + pad,
    ]


def _prep_heatmap_layer(spec, bbox_nwse, region_polygon, extent):
    _ensure_layer_dataset(spec)
    ds = spec.ds
    variable = _layer_variable(ds, spec)
    ds = to_standard_units(ds, variables=[variable])
    ds = precip_for_display(ds, variable)
    da = ds[variable]
    overrides = _layer_overrides(spec, spec.options.get("index"))
    da, lat_dim, lon_dim, extent_vals, wrap_lon, native_step_dim, native_steps = (
        _prepare_gridded_map(
            da,
            overrides,
            bbox_nwse,
            None,
            extent,
            style="heatmap",
            region_polygon=region_polygon,
        )
    )
    if wrap_lon:
        da = ensure_normalized_longitude(da, lon_dim)
    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)
    flag_scale = _discrete_flag_scale(da, spec.options.get("colormap"))
    if flag_scale is not None:
        cmap, norm, flag_ticks, flag_labels = flag_scale
        vmin = vmax = None
    else:
        cmap, norm = _heatmap_scale(da, spec.options.get("colormap"))
        flag_ticks = flag_labels = None
        vmin = vmax = None
        if norm is None:
            vmax = float(da.max(skipna=True).values)
            vmin = float(da.min(skipna=True).values)
            if vmax > 0 and vmin < 0:
                m = max(abs(vmax), abs(vmin))
                vmin, vmax = -m, m
    return {
        "kind": "heatmap",
        "spec": spec,
        "da": da,
        "lat_dim": lat_dim,
        "lon_dim": lon_dim,
        "cmap": cmap,
        "norm": norm,
        "vmin": vmin,
        "vmax": vmax,
        "flag_ticks": flag_ticks,
        "flag_labels": flag_labels,
        "wrap_lon": wrap_lon,
        "native_step_dim": native_step_dim,
        "native_steps": native_steps,
        "panel_dim": _step_dim(da),
        "cbar_label": _variable_label(da),
        "variable": variable,
        "units": variable_units(da),
        "zorder": _KIND_ZORDER["heatmap"],
    }


def _prep_scatter_layer(spec, bbox_nwse, region_polygon):
    _ensure_layer_dataset(spec)
    ds = spec.ds
    point_dim = _point_dim(ds)
    if point_dim is None:
        raise UsageError(
            f"--layer scatter:{spec.path} needs a station_id or point_id dimension "
            f"(got dims {list(ds.dims)})"
        )
    variable = _layer_variable(ds, spec)
    ds = to_standard_units(ds, variables=[variable])
    ds = precip_for_display(ds, variable)
    da = ds[variable]
    overrides = _layer_overrides(spec, spec.options.get("index"))
    panel_dim = _step_dim(da)
    da = _apply_index(da, overrides, list_dims=(panel_dim,) if panel_dim else ())
    if bbox_nwse is not None or region_polygon is not None:
        da = _subset_points(da, bbox_nwse, region_polygon)
    extra = [d for d in da.dims if d not in (panel_dim, point_dim) and d is not None]
    extra = [d for d in extra if d in da.dims]
    if extra:
        if extra == ["number"] or (len(extra) == 1 and extra[0] == "number"):
            da = da.mean("number", keep_attrs=True)
        else:
            raise UsageError(
                f"--layer scatter:{spec.path} still has dimension(s) {extra}; "
                "select a position with index= or reduce them first"
            )
    cmap, norm = _heatmap_scale(da, spec.options.get("colormap"))
    vmin = vmax = None
    if norm is None:
        vmax = float(da.max(skipna=True).values)
        vmin = float(da.min(skipna=True).values)
        if vmax > 0 and vmin < 0:
            m = max(abs(vmax), abs(vmin))
            vmin, vmax = -m, m
    return {
        "kind": "scatter",
        "spec": spec,
        "da": da,
        "ds": ds,
        "point_dim": point_dim,
        "cmap": cmap,
        "norm": norm,
        "vmin": vmin,
        "vmax": vmax,
        "panel_dim": _step_dim(da),
        "cbar_label": _variable_label(da),
        "variable": variable,
        "units": variable_units(da),
        "zorder": _KIND_ZORDER["scatter"],
    }


def _prep_quiver_layer(spec, bbox_nwse, region_polygon, extent):
    _ensure_layer_dataset(spec)
    ds = spec.ds
    u_name, v_name = _resolve_uv(ds, spec.options.get("u-variable"), spec.options.get("v-variable"))
    ds = to_standard_units(ds, variables=[u_name, v_name])
    u_da = ds[u_name]
    v_da = ds[v_name]
    u_units = variable_units(u_da)
    v_units = variable_units(v_da)
    if u_units and v_units and not units_equal(u_units, v_units):
        raise UsageError(f"u units {u_units!r} do not match v units {v_units!r}")
    overrides = _layer_overrides(spec, spec.options.get("index"))
    u_da, lat_dim, lon_dim, extent_vals, wrap_lon, native_step_dim, native_steps = (
        _prepare_gridded_map(
            u_da,
            overrides,
            bbox_nwse,
            None,
            extent,
            style="quiver",
            region_polygon=region_polygon,
        )
    )
    v_da, *_ = _prepare_gridded_map(
        v_da,
        overrides,
        bbox_nwse,
        None,
        extent,
        style="quiver",
        region_polygon=region_polygon,
    )
    if wrap_lon:
        u_da = ensure_normalized_longitude(u_da, lon_dim)
        v_da = ensure_normalized_longitude(v_da, lon_dim)
    if "number" in u_da.dims:
        u_da = u_da.mean("number", keep_attrs=True)
        v_da = v_da.mean("number", keep_attrs=True)
    speed = _wind_speed_da(u_da, v_da)
    cmap = (
        _parse_colormap(spec.options.get("colormap"))
        if spec.options.get("colormap")
        else QUIVER_CMAP
    )
    qscale = spec.options.get("quiver-scale")
    qstep = spec.options.get("quiver-step")
    return {
        "kind": "quiver",
        "spec": spec,
        "speed": speed,
        "u_da": u_da,
        "v_da": v_da,
        "lat_dim": lat_dim,
        "lon_dim": lon_dim,
        "cmap": cmap,
        "norm": None,
        "vmin": float(speed.min(skipna=True).values),
        "vmax": float(speed.max(skipna=True).values),
        "wrap_lon": wrap_lon,
        "native_step_dim": native_step_dim,
        "native_steps": native_steps,
        "panel_dim": _step_dim(speed),
        "cbar_label": _wind_speed_cbar_label(u_da),
        "variable": "speed",
        "units": variable_units(u_da),
        "quiver_scale": float(qscale) if qscale is not None else None,
        "quiver_step": int(qstep) if qstep is not None else None,
        "zorder": _KIND_ZORDER["quiver"],
        "draw_mesh": False,
    }


def _prep_outline_layer(spec):
    return {
        "kind": "outline",
        "spec": spec,
        "polygon": polygon_from_geojson(spec.path, flag="--layer outline"),
        "panel_dim": None,
        "zorder": _KIND_ZORDER["outline"],
    }


def _layer_field(prepared):
    if prepared["kind"] == "quiver":
        return prepared.get("speed")
    return prepared.get("da")


def _sel_layer(prepared, dim, labels):
    if prepared["kind"] == "heatmap":
        prepared["da"] = prepared["da"].sel({dim: labels})
    elif prepared["kind"] == "scatter":
        prepared["da"] = prepared["da"].sel({dim: labels})
    elif prepared["kind"] == "quiver":
        prepared["speed"] = prepared["speed"].sel({dim: labels})
        prepared["u_da"] = prepared["u_da"].sel({dim: labels})
        prepared["v_da"] = prepared["v_da"].sel({dim: labels})


def _squeeze_layer_dim(prepared, dim):
    field = _layer_field(prepared)
    if field is None or dim not in getattr(field, "dims", ()):
        return
    if field.sizes[dim] != 1:
        return
    if prepared["kind"] == "heatmap":
        prepared["da"] = prepared["da"].squeeze(dim, drop=True)
    elif prepared["kind"] == "scatter":
        prepared["da"] = prepared["da"].squeeze(dim, drop=True)
    elif prepared["kind"] == "quiver":
        prepared["speed"] = prepared["speed"].squeeze(dim, drop=True)
        prepared["u_da"] = prepared["u_da"].squeeze(dim, drop=True)
        prepared["v_da"] = prepared["v_da"].squeeze(dim, drop=True)
    prepared["panel_dim"] = None


def _select_panel(prepared, label):
    """Return a copy of ``prepared`` reduced to one panel label, or the original if static."""
    dim = prepared.get("panel_dim")
    if dim is None or label is None:
        return prepared
    out = dict(prepared)
    if prepared["kind"] == "heatmap":
        out["da"] = prepared["da"].sel({dim: label})
    elif prepared["kind"] == "scatter":
        out["da"] = prepared["da"].sel({dim: label})
    elif prepared["kind"] == "quiver":
        out["speed"] = prepared["speed"].sel({dim: label})
        out["u_da"] = prepared["u_da"].sel({dim: label})
        out["v_da"] = prepared["v_da"].sel({dim: label})
    out["panel_dim"] = None
    return out


def _draw_heatmap_on_ax(ax, prepared, transform):
    da = _plain(prepared["da"])
    lat_dim, lon_dim = prepared["lat_dim"], prepared["lon_dim"]
    slab = da.transpose(lat_dim, lon_dim)
    return ax.pcolormesh(
        slab[lon_dim],
        slab[lat_dim],
        slab.values,
        cmap=prepared["cmap"],
        norm=prepared["norm"],
        vmin=prepared["vmin"],
        vmax=prepared["vmax"],
        transform=transform,
        zorder=prepared["zorder"],
    )


def _draw_scatter_on_ax(ax, prepared, transform):
    da = _plain(prepared["da"])
    lat_name = cf_dim(da, "latitude")
    lon_name = cf_dim(da, "longitude")
    return ax.scatter(
        da[lon_name].values,
        da[lat_name].values,
        c=da.values,
        cmap=prepared["cmap"],
        norm=prepared["norm"],
        vmin=prepared["vmin"],
        vmax=prepared["vmax"],
        s=30,
        transform=transform,
        zorder=prepared["zorder"],
        edgecolors="k",
        linewidths=0.3,
    )


def _draw_quiver_on_ax(ax, prepared, transform, scale, step):
    u_da = _plain(prepared["u_da"])
    v_da = _plain(prepared["v_da"])
    lat_dim, lon_dim = prepared["lat_dim"], prepared["lon_dim"]
    u_slab = u_da.transpose(lat_dim, lon_dim)
    v_slab = v_da.transpose(lat_dim, lon_dim)
    mesh = None
    if prepared.get("draw_mesh"):
        speed = _plain(prepared["speed"]).transpose(lat_dim, lon_dim)
        mesh = ax.pcolormesh(
            speed[lon_dim],
            speed[lat_dim],
            speed.values,
            cmap=prepared["cmap"],
            vmin=prepared["vmin"],
            vmax=prepared["vmax"],
            transform=transform,
            zorder=1.0,
        )
    lon_q, lat_q, u_q, v_q = _subsample_quiver(
        u_slab[lon_dim].values,
        u_slab[lat_dim].values,
        u_slab.values,
        v_slab.values,
        step,
    )
    quiv = ax.quiver(
        lon_q,
        lat_q,
        u_q,
        v_q,
        transform=transform,
        scale=scale,
        color="k",
        zorder=prepared["zorder"],
    )
    return mesh, quiv


def _draw_outline_on_ax(ax, prepared, crs):
    ax.add_geometries(
        [prepared["polygon"]],
        crs,
        facecolor="none",
        edgecolor="black",
        linewidth=1.2,
        zorder=prepared["zorder"],
    )


def _scale_groups(prepared_layers, shared_scale, independent_scale):
    """Return True if heatmap/scatter layers should share one color scale."""
    data = [p for p in prepared_layers if p["kind"] in ("heatmap", "scatter")]
    if len(data) < 2:
        return False
    if independent_scale:
        return False
    if shared_scale:
        return True
    variables = {p["variable"] for p in data}
    units = {p["units"] for p in data if p["units"]}
    return len(variables) == 1 and len(units) <= 1


def _apply_shared_scale(prepared_layers):
    data = [p for p in prepared_layers if p["kind"] in ("heatmap", "scatter")]
    if not data:
        return
    cmap, norm = data[0]["cmap"], data[0]["norm"]
    if norm is None:
        vmins = [p["vmin"] for p in data if p["vmin"] is not None]
        vmaxs = [p["vmax"] for p in data if p["vmax"] is not None]
        vmin = min(vmins) if vmins else None
        vmax = max(vmaxs) if vmaxs else None
        if vmin is not None and vmax is not None and vmax > 0 and vmin < 0:
            m = max(abs(vmax), abs(vmin))
            vmin, vmax = -m, m
    else:
        vmin = vmax = None
    for p in data:
        p["cmap"] = cmap
        p["norm"] = norm
        p["vmin"] = vmin
        p["vmax"] = vmax


def _plot_layers(
    layers,
    bbox_nwse,
    mask_geojson,
    extent,
    cities,
    title,
    fontsize,
    draw_boxes,
    rows,
    columns,
    variable,
    colormap,
    index,
    u_variable,
    v_variable,
    quiver_scale,
    quiver_step,
    shared_scale,
    independent_scale,
    layer_labels=None,
    xlabel=None,
    ylabel=None,
):
    """Stack ``--layer`` entries on shared Cartopy panels."""
    import cartopy.crs as ccrs
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm

    if shared_scale and independent_scale:
        raise UsageError("--shared-scale and --independent-scale are mutually exclusive")

    label_slots = resolve_input_labels(layer_labels, len(layers), input_flag="--layer")

    inherited = []
    for spec in layers:
        opts = dict(spec.options)
        if "variable" not in opts and variable:
            opts["variable"] = variable
        if "colormap" not in opts and colormap:
            opts["colormap"] = colormap
        if "index" not in opts and index:
            opts["index"] = index
        if "u-variable" not in opts and u_variable:
            opts["u-variable"] = u_variable
        if "v-variable" not in opts and v_variable:
            opts["v-variable"] = v_variable
        if "quiver-scale" not in opts and quiver_scale is not None:
            opts["quiver-scale"] = str(quiver_scale)
        if "quiver-step" not in opts and quiver_step is not None:
            opts["quiver-step"] = str(quiver_step)
        inherited.append(_copy_layer(spec, opts))

    region_polygon = _combined_mask_polygon(mask_geojson, inherited)
    extent_vals = _parse_extent(extent)
    prepared = []
    for i, spec in enumerate(inherited):
        if spec.kind == "mask":
            continue
        if spec.kind == "heatmap":
            item = _prep_heatmap_layer(spec, bbox_nwse, region_polygon, extent)
        elif spec.kind == "scatter":
            item = _prep_scatter_layer(spec, bbox_nwse, region_polygon)
        elif spec.kind == "quiver":
            item = _prep_quiver_layer(spec, bbox_nwse, region_polygon, extent)
        elif spec.kind == "outline":
            item = _prep_outline_layer(spec)
        else:
            raise UsageError(f"unknown --layer kind {spec.kind!r}")
        label_override = label_slots[i]
        if label_override:
            item["cbar_label"] = label_override
        elif spec.ds is not None and spec.kind in {"heatmap", "scatter", "quiver"}:
            item["cbar_label"] = dataset_display_label(
                spec.ds, item.get("cbar_label") or spec.path
            )
        item["zorder"] = item["zorder"] + i * 0.01
        prepared.append(item)

    if not prepared:
        raise UsageError("--layer needs at least one heatmap, scatter, quiver, or outline")

    has_heatmap = any(p["kind"] == "heatmap" for p in prepared)
    for p in prepared:
        if p["kind"] == "quiver":
            p["draw_mesh"] = not has_heatmap

    driver = next((p for p in prepared if p.get("panel_dim")), None)
    if driver is None:
        steps = [None]
        sdim = None
        title_da = None
        title_steps = [None]
    else:
        sdim = driver["panel_dim"]
        title_da = _layer_field(driver)
        driver_values = list(title_da[sdim].values)
        driver_kind = _axis_kind(title_da[sdim].values)
        aligned = driver_values
        for p in prepared:
            if p is driver:
                continue
            field = _layer_field(p)
            if field is None:
                continue
            other_dim, labels = _align_panel_labels(sdim, aligned, driver_kind, field, p["spec"])
            if other_dim is None:
                continue
            aligned = labels
            p["panel_dim"] = other_dim
        if not aligned:
            raise UsageError("no overlapping time bins across --layer inputs")
        for p in prepared:
            field = _layer_field(p)
            dim = p.get("panel_dim")
            if field is None or dim is None or dim not in field.dims:
                continue
            _sel_layer(p, dim, aligned)
        title_da = _layer_field(driver)
        steps = list(title_da[sdim].values) if sdim in title_da.dims else aligned
        if sdim is not None and title_da.sizes.get(sdim, 1) == 1:
            for p in prepared:
                _squeeze_layer_dim(p, p.get("panel_dim"))
            steps = [None]
            sdim = None
        native = driver.get("native_steps")
        native_dim = driver.get("native_step_dim")
        title_steps = native if native is not None and native_dim == sdim else steps

    for p in prepared:
        dim = p.get("panel_dim")
        field = _layer_field(p)
        if sdim is None and dim and field is not None and dim in field.dims:
            raise UsageError(
                f"--layer {p['spec'].kind}:{p['spec'].path} still has {dim!r}; "
                "select a position with index= (the other layers have no panel axis)"
            )

    if extent_vals is None:
        if bbox_nwse is not None:
            r_n, r_w, r_s, r_e = bbox_nwse
            extent_vals = [float(r_w), float(r_e), float(r_s), float(r_n)]
        else:
            extent_vals = None
            for p in prepared:
                if p["kind"] in ("heatmap", "quiver"):
                    src = p["da"] if p["kind"] == "heatmap" else p["speed"]
                    extent_vals = _extent_from_field(src, p["lat_dim"], p["lon_dim"])
                    break
            if extent_vals is None:
                scatter = next((p for p in prepared if p["kind"] == "scatter"), None)
                if scatter is not None:
                    extent_vals = _extent_from_points(scatter["da"])
                else:
                    raise UsageError("could not determine map extent; pass --extent or --bbox")

    wrap_lon = True
    for p in prepared:
        if "wrap_lon" in p:
            wrap_lon = p["wrap_lon"]
            break

    share = _scale_groups(prepared, shared_scale, independent_scale)
    if share:
        _apply_shared_scale(prepared)

    num_steps = len(steps)
    nrows, ncols = _panel_shape(num_steps, rows=rows, columns=columns)
    sw, sh = _figsize_from_extent(*extent_vals)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(sw * ncols, sh * nrows),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
    )
    axes = np.array(axes).reshape(nrows, ncols).flatten()
    overlays = _load_geo_overlays(extent_vals)
    cities_map = _parse_cities(cities)
    boxes = draw_boxes or []
    transform = ccrs.PlateCarree()

    quiver_meta = None
    for p in prepared:
        if p["kind"] == "quiver":
            step = _quiver_step(
                p["u_da"][p["lat_dim"]].values, p["u_da"][p["lon_dim"]].values, p.get("quiver_step")
            )
            native_spacing = _native_spacing_deg(
                p["u_da"][p["lat_dim"]].values, p["u_da"][p["lon_dim"]].values
            )
            arrow_spacing = None if native_spacing is None else native_spacing * step
            lon_span = abs(extent_vals[1] - extent_vals[0])
            scale = _auto_quiver_scale(
                p["u_da"].values,
                p["v_da"].values,
                lon_span,
                arrow_spacing,
                requested=p.get("quiver_scale"),
            )
            quiver_meta = (p, scale, step)
            break

    last_by_group = {}
    last_quiv = None
    for i, s in enumerate(steps):
        ax = axes[i]
        if wrap_lon:
            ax.set_extent(extent_vals, crs=transform)
        else:
            ax.set_xlim(extent_vals[0], extent_vals[1])
            ax.set_ylim(extent_vals[2], extent_vals[3])
        for p in prepared:
            slab = _select_panel(p, s)
            if slab["kind"] == "heatmap":
                last_by_group.setdefault(id(p) if not share else "shared", None)
                artist = _draw_heatmap_on_ax(ax, slab, transform)
                last_by_group["shared" if share else id(p)] = (artist, p)
            elif slab["kind"] == "scatter":
                artist = _draw_scatter_on_ax(ax, slab, transform)
                last_by_group["shared" if share else id(p)] = (artist, p)
            elif slab["kind"] == "quiver":
                _, scale, step = quiver_meta
                mesh, quiv = _draw_quiver_on_ax(ax, slab, transform, scale, step)
                last_quiv = quiv
                if mesh is not None:
                    last_by_group[id(p)] = (mesh, p)
            elif slab["kind"] == "outline":
                _draw_outline_on_ax(ax, slab, transform)
        _draw_geo_overlays(ax, overlays, transform)
        gl = ax.gridlines(draw_labels=True, alpha=0)
        gl.top_labels = False
        gl.right_labels = False
        ax.set_xlabel(_resolve_axis_label(xlabel, "Longitude"))
        ax.set_ylabel(_resolve_axis_label(ylabel, "Latitude"))
        for city, (lat, lon) in cities_map.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=transform, zorder=8)
            ax.text(
                lon - 2.0,
                lat + 0.5,
                city,
                fontsize=max(10, int(round(fontsize * 0.65))),
                transform=transform,
                zorder=8,
            )
        if boxes:
            _draw_boxes_on_ax(ax, boxes, transform)
        if s is not None and title_da is not None:
            ax.set_title(
                _panel_title(title_da, sdim, s, title_steps),
                fontsize=_panel_title_fontsize(fontsize),
            )

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    if last_quiv is not None:
        last = axes[num_steps - 1]
        qlayer = next(p for p in prepared if p["kind"] == "quiver")
        units_disp = _speed_units_display(qlayer["u_da"])
        y_key = 0.18
        for u_ref in QUIVER_KEY_MS:
            last.quiverkey(
                last_quiv,
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

    cbars = list(last_by_group.values())
    n_cbars = len(cbars)
    for ci, (mappable, p) in enumerate(cbars):
        cbar_ax = _map_colorbar_axes(fig, title=title, nrows=nrows, index=ci, n_cbars=n_cbars)
        cbar = fig.colorbar(
            mappable,
            cax=cbar_ax,
            orientation="horizontal",
            fraction=5,
            **_cbar_boundary_kwargs(p.get("norm"), p.get("cmap")),
        )
        if p.get("flag_ticks") is not None:
            cbar.set_ticks(p["flag_ticks"])
            if p.get("flag_labels") is not None:
                cbar.set_ticklabels(p["flag_labels"])
        cbar_label_fs, cbar_tick_fs = _colorbar_text_sizes(fontsize)
        cbar.set_label(
            p.get("cbar_label") or _variable_label(p.get("da")),
            fontsize=cbar_label_fs,
        )
        cbar.ax.tick_params(labelsize=cbar_tick_fs)
    return fig


def _contour_levels(vmin, vmax, n=10, norm=None):
    """Shared isoline edges for every contour panel (and a constant-field pad)."""
    import numpy as np

    boundaries = getattr(norm, "boundaries", None) if norm is not None else None
    if boundaries is not None:
        return list(boundaries)
    if vmin is None or vmax is None or not np.isfinite(vmin) or not np.isfinite(vmax):
        return n
    if vmin == vmax:
        pad = abs(vmin) * 0.05 if vmin != 0 else 1.0
        return np.linspace(vmin - pad, vmax + pad, n + 1)
    return np.linspace(vmin, vmax, n + 1)


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
    kind="heatmap",
    xlabel=None,
    ylabel=None,
):
    import cartopy.crs as ccrs
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm

    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)

    if wrap_lon:
        da = ensure_normalized_longitude(da, lon_dim)
    if kind == "contour":
        lat_vals = np.asarray(da[lat_dim].values)
        if lat_vals.size > 1 and float(lat_vals[0]) > float(lat_vals[-1]):
            da = da.sortby(lat_dim)

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
        extent = _extent_from_field(da, lat_dim, lon_dim)

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

    mappable = None
    boxes = draw_boxes or []
    overlays = _load_geo_overlays(extent)
    levels = _contour_levels(vmin, vmax, norm=norm) if kind == "contour" else None
    contour_extend = (
        "both"
        if getattr(cmap, "name", None) in ("chirps_anom", "chirps_total", "chirps_short")
        else "neither"
    )
    for i, s in enumerate(steps):
        ax = axes[i]
        slab = da if sdim is None else da.isel({sdim: i})
        slab = slab.transpose(lat_dim, lon_dim)
        lon_1d = slab[lon_dim]
        lat_1d = slab[lat_dim]
        if kind == "contour":
            mappable = ax.contourf(
                lon_1d,
                lat_1d,
                slab.values,
                levels=levels,
                cmap=cmap,
                norm=norm,
                transform=ccrs.PlateCarree(),
                extend=contour_extend,
            )
            ax.contour(
                lon_1d,
                lat_1d,
                slab.values,
                levels=levels,
                colors="k",
                linewidths=0.4,
                transform=ccrs.PlateCarree(),
            )
        else:
            mappable = ax.pcolormesh(
                lon_1d,
                lat_1d,
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
        ax.set_xlabel(_resolve_axis_label(xlabel, "Longitude"))
        ax.set_ylabel(_resolve_axis_label(ylabel, "Latitude"))
        for city, (lat, lon) in cities.items():
            ax.plot(lon, lat, marker="o", color="k", markersize=6, transform=ccrs.PlateCarree())
            ax.text(
                lon - 2.0,
                lat + 0.5,
                city,
                fontsize=max(10, int(round(fontsize * 0.65))),
                transform=ccrs.PlateCarree(),
            )
        if boxes:
            _draw_boxes_on_ax(ax, boxes, ccrs.PlateCarree())
        if s is not None:
            ax.set_title(
                _panel_title(da, sdim, s, title_steps),
                fontsize=_panel_title_fontsize(fontsize),
            )

    for j in range(num_steps, len(axes)):
        axes[j].set_visible(False)

    if title:
        fig.suptitle(title, fontsize=fontsize)
    cbar_ax = _map_colorbar_axes(fig, title=title, nrows=nrows)
    cbar = fig.colorbar(
        mappable,
        cax=cbar_ax,
        orientation="horizontal",
        fraction=5,
        **_cbar_boundary_kwargs(norm, cmap),
    )
    if flag_ticks is not None:
        cbar.set_ticks(flag_ticks)
        if flag_labels is not None:
            cbar.set_ticklabels(flag_labels)
    cbar_label_fs, cbar_tick_fs = _colorbar_text_sizes(fontsize)
    cbar.set_label(_variable_label(da), fontsize=cbar_label_fs)
    cbar.ax.tick_params(labelsize=cbar_tick_fs)
    return fig


@weather_skill(
    name="plot",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=False, default=None)
@weather_skill.argument(
    "--x",
    dest="x_ds",
    type=Dataset("any"),
    required=False,
    default=None,
    help="X-axis Zarr for --style xy. Mutually exclusive with -i/--input.",
)
@weather_skill.argument(
    "--y",
    dest="y_ds",
    type=Dataset("any"),
    required=False,
    default=None,
    help="Y-axis Zarr for --style xy. Mutually exclusive with -i/--input.",
)
@weather_skill.argument(
    "--layer",
    action="append",
    default=None,
    type=parse_layer,
    help=(
        "Map layer KIND:PATH or KIND:PATH::k=v. Repeat for overlays. "
        "Kinds: heatmap, scatter, quiver, outline, mask. "
        "Options: variable, colormap, index, u-variable, v-variable, "
        "quiver-scale, quiver-step. Mutually exclusive with -i/--input."
    ),
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--x-variable",
    default=None,
    help="X-axis variable for --style xy. Defaults to the first data variable of --x (or -i).",
)
@weather_skill.argument(
    "--y-variable",
    default=None,
    help="Y-axis variable for --style xy. Defaults to the first data variable of --y (or -i).",
)
@weather_skill.argument(
    "--pair-on",
    choices=["time", "year", "index"],
    default="time",
    help=(
        "How --style xy matches --x to --y samples: shared time (default), "
        "calendar year (e.g. September IOD vs October rain), or position."
    ),
)
@weather_skill.argument(
    "--style",
    choices=["heatmap", "contour", "timeseries", "xy", "windrose", "quiver"],
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
        "Heatmap default: discrete CHIRPS-GEFS precip classes for precip "
        "variables, else viridis. Windrose default: blue-to-orange speed classes. "
        "Quiver default: YlGn (ECMWF S2S 10 m / 700 hPa wind vectors)."
    ),
)
@weather_skill.argument(
    "--index",
    default=None,
    help=(
        "Slice like 'step=3,number=0' (heatmap, contour, quiver, and windrose). "
        "Heatmap/contour/quiver lists keep the dim as panels; windrose lists keep samples."
    ),
)
@weather_skill.argument(
    "--extent",
    default=None,
    help="Map extent 'lon_min,lon_max,lat_min,lat_max' (heatmap, contour, and quiver).",
)
@weather_skill.argument(
    "--cities",
    default=None,
    help='City overlay JSON (heatmap, contour, and quiver). Inline {"name": [lat, lon]} or file path.',
)
@weather_skill.argument(
    "--fontsize",
    type=int,
    default=18,
    help="Base font size for titles, axis labels, and colorbar text (default 18).",
)
@weather_skill.argument("--title", default=None, help="Optional plot title.")
@weather_skill.argument(
    "--xlabel",
    default=None,
    help="Override the x-axis label (default: Longitude / Valid time / …).",
)
@weather_skill.argument(
    "--ylabel",
    default=None,
    help="Override the y-axis label (default: Latitude / variable label / …).",
)
@weather_skill.argument(
    "--rows",
    type=int,
    default=None,
    help=(
        "Heatmap/contour/quiver panel rows. Alone or with --columns, the grid must pack the data exactly."
    ),
)
@weather_skill.argument(
    "--columns",
    type=int,
    default=None,
    help=(
        "Heatmap/contour/quiver panel columns. Alone or with --rows, the grid must pack the data exactly."
    ),
)
@weather_skill.argument(
    "--mask-geojson",
    default=None,
    help="GeoJSON polygon; cells/points outside become NaN (heatmap, contour, quiver, windrose, xy).",
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
        "Matplotlib quiver scale (larger → shorter arrows). "
        "Default sizes a typical wind to ~1.5× the subsampled grid spacing. "
        "Quiver-only."
    ),
)
@weather_skill.argument(
    "--quiver-step",
    type=int,
    default=None,
    help=(
        "Plot every Nth grid point for --style quiver "
        "(S2S plot_wind_and_sst_anomaly quiver_step). "
        "Default: 1 on ~1.5° grids; finer grids auto-thin to ~1.5°. Quiver-only."
    ),
)
@weather_skill.argument(
    "--label",
    action="append",
    default=None,
    help="Colorbar label for each --layer, in order. Omit to infer from metadata.",
)
@weather_skill.argument(
    "--shared-scale",
    action="store_true",
    help="Force one shared color scale across heatmap/scatter layers.",
)
@weather_skill.argument(
    "--independent-scale",
    action="store_true",
    help="Force a separate color scale per heatmap/scatter layer.",
)
def plot(
    ds,
    bbox,
    variable,
    style,
    colormap,
    title,
    xlabel,
    ylabel,
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
    quiver_step,
    output,
    layer=None,
    label=None,
    shared_scale=False,
    independent_scale=False,
    x_ds=None,
    y_ds=None,
    x_variable=None,
    y_variable=None,
    pair_on="time",
    **kwargs,
):
    """Render a heatmap, contour, timeseries, xy scatter, wind-rose, quiver, or layered map PNG from weather-skills Zarrs."""
    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter

    layers = layer or []
    if layers and ds is not None:
        raise UsageError("pass either -i/--input or --layer, not both")
    if layers and (x_ds is not None or y_ds is not None):
        raise UsageError("pass either --layer or --x/--y, not both")
    if ds is not None and (x_ds is not None or y_ds is not None):
        raise UsageError("pass either -i/--input or --x/--y, not both")
    if style == "xy":
        if layers:
            raise UsageError("--layer cannot be used with --style xy")
        if x_ds is None and y_ds is None:
            if ds is None:
                raise UsageError(
                    "--style xy needs --x and --y, or -i with --x-variable and --y-variable"
                )
            if not x_variable or not y_variable:
                raise UsageError(
                    "with a single -i, --style xy needs both --x-variable and --y-variable"
                )
            x_ds = ds
            y_ds = ds
        elif x_ds is None or y_ds is None:
            raise UsageError("--style xy needs both --x and --y")
    elif not layers and ds is None:
        raise UsageError("pass -i/--input or at least one --layer")
    if layers and style in ("timeseries", "xy", "windrose", "contour"):
        raise UsageError(f"--layer cannot be used with --style {style}")
    if layers and style == "quiver":
        raise UsageError(
            "with --layer, draw wind vectors as --layer quiver:PATH instead of --style quiver"
        )

    try:
        overrides = _parse_index(index)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    bbox_nwse = bbox
    draw_boxes = _parse_draw_boxes(draw_box)

    if layers:
        fig = _plot_layers(
            layers,
            bbox_nwse,
            mask_geojson,
            extent,
            cities,
            title,
            fontsize,
            draw_boxes,
            rows,
            columns,
            variable,
            colormap,
            index,
            u_variable,
            v_variable,
            quiver_scale,
            quiver_step,
            shared_scale,
            independent_scale,
            layer_labels=label,
            xlabel=xlabel,
            ylabel=ylabel,
        )
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return output
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
        "--quiver-step": quiver_step is not None,
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
                    f"Warning: {flag}{_flag_detail(flag)} is a map-only option; "
                    f"ignored for --style {style}.",
                    file=sys.stderr,
                )
        for flag, set_ in {**uv_flags, **quiver_only}.items():
            if set_:
                print(
                    f"Warning: {flag} is ignored for --style {style}.",
                    file=sys.stderr,
                )
    elif style == "xy":
        for flag, set_ in map_only.items():
            if set_:
                print(
                    f"Warning: {flag}{_flag_detail(flag)} is a map-only option; "
                    f"ignored for --style {style}.",
                    file=sys.stderr,
                )
        for flag, set_ in {**uv_flags, **quiver_only}.items():
            if set_:
                print(
                    f"Warning: {flag} is ignored for --style {style}.",
                    file=sys.stderr,
                )
        if variable:
            print(
                "Warning: --variable is ignored for --style xy; "
                "use --x-variable/--y-variable.",
                file=sys.stderr,
            )
    elif style in ("heatmap", "contour"):
        for flag, set_ in {**uv_flags, **quiver_only}.items():
            if set_:
                print(
                    f"Warning: {flag} is only used with --style windrose or "
                    f"--style quiver; ignored for --style {style}.",
                    file=sys.stderr,
                )
    elif style == "windrose":
        for flag, set_ in {**map_only, **quiver_only}.items():
            if set_:
                print(
                    f"Warning: {flag}{_flag_detail(flag)} is ignored for --style windrose.",
                    file=sys.stderr,
                )

    if style == "xy":
        fig = _plot_xy(
            x_ds,
            y_ds,
            x_variable,
            y_variable,
            pair_on,
            overrides,
            bbox_nwse,
            mask_geojson,
            title,
            xlabel,
            ylabel,
            fontsize,
        )
    elif style == "windrose":
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
            quiver_step,
            xlabel=xlabel,
            ylabel=ylabel,
        )
    else:
        variable = variable or auto_variable(ds)
        if not variable or variable not in ds:
            raise UsageError(f"no usable variable. Available: {list(ds.data_vars)}")
        ds = to_standard_units(ds, variables=[variable])
        ds = precip_for_display(ds, variable)
        da = ds[variable]

    if style in ("heatmap", "contour"):
        (
            da,
            lat_dim,
            lon_dim,
            extent_vals,
            wrap_lon,
            native_step_dim,
            native_steps,
        ) = _prepare_gridded_map(da, overrides, bbox_nwse, mask_geojson, extent, style=style)
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
            kind=style,
            xlabel=xlabel,
            ylabel=ylabel,
        )
    elif style == "timeseries":
        fig, ax = plt.subplots(figsize=(10, 6))
        sdim = "step" if "step" in da.dims else cf_dim(da, "time")
        if sdim is None:
            raise UsageError(f"timeseries needs 'step' or 'time'; got {list(da.dims)}.")
        reduce_dims = [d for d in da.dims if d != sdim]
        reduced = da.mean(reduce_dims, keep_attrs=True)
        xvals, default_xlabel = _timeseries_axis(reduced, sdim)
        ax.plot(xvals, reduced.values, marker="o", markersize=5)
        tick_fs = max(10, int(round(fontsize * 0.7)))
        resolved_xlabel = _resolve_axis_label(xlabel, default_xlabel)
        ax.set_xlabel(resolved_xlabel, fontsize=fontsize)
        ax.set_ylabel(
            _resolve_axis_label(ylabel, _variable_label(reduced)),
            fontsize=fontsize,
        )
        qty = variable_label_for_display(reduced, include_units=False)
        ax.set_title(title or f"{qty} ({style})", fontsize=fontsize)
        ax.tick_params(labelsize=tick_fs)
        if resolved_xlabel == "Valid time" or default_xlabel == "Valid time":
            fig.autofmt_xdate()
        fig.tight_layout()

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot()
