# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cartopy",
#   "cf-xarray",
#   "cftime",
#   # matplotlib<3.10: cartopy gridliner crash
#   "matplotlib>=3.8,<3.10",
#   "numpy",
#   "shapely>=2.1",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""Compare two or more gridded datasets as a heatmap grid PNG."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.display_labels import dataset_display_label, resolve_input_labels
from weather_skills_core.standard_utils import (
    ensure_normalized_longitude,
    lat_slice,
    pick_time_dim,
    polygon_from_geojson,
)
from weather_skills_core.units import (
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
PRECIP_LONG_MIN_DAYS = 5

# CHIRPS-GEFS / Early Warning eXplorer rainfall-anomaly classes (mm).
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


_NS_PER_DAY = 86_400_000_000_000
_TOL_NS = 1_000_000_000  # 1 s, matching plot-compare


def _scaled_fontsize(base, frac, *, floor=8):
    """Scale a base ``--fontsize`` by ``frac``, never below ``floor``."""
    return max(floor, int(round(int(base) * frac)))


def _parse_colormap(spec):
    if spec is None or "," not in spec:
        return spec
    from matplotlib.colors import LinearSegmentedColormap

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    return LinearSegmentedColormap.from_list("custom", parts)


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
    kind = classify_variable(
        da.name or "",
        units=variable_units(da),
        standard_name=da.attrs.get("standard_name"),
    )
    if kind in ("precip", "precip_amount"):
        if _is_precip_anomaly(da):
            return _precip_anomaly_scale()
        return _precip_scale(da)
    return "viridis", None


def _cbar_boundary_kwargs(norm, cmap=None):
    from matplotlib.colors import BoundaryNorm

    if not isinstance(norm, BoundaryNorm):
        return {}
    kw = {"spacing": "uniform", "ticks": list(norm.boundaries)}
    if getattr(cmap, "name", None) in ("chirps_anom", "chirps_total", "chirps_short"):
        kw["extend"] = "both"
    return kw


_MAP_CBAR_LEFT = 0.08
_MAP_CBAR_WIDTH = 0.84
_MAP_CBAR_HEIGHT = 0.045
_CBAR_INCHES_PER_TICK = 0.48


def _colorbar_tick_count(norm):
    from matplotlib.colors import BoundaryNorm

    if not isinstance(norm, BoundaryNorm):
        return 0
    return len(list(norm.boundaries))


def _colorbar_figure_width(ncols, n_ticks):
    """Physical figure width so discrete colorbar labels do not collide."""
    col_width = max(3.2 * ncols, 6.0)
    if n_ticks < 8:
        return col_width
    needed = (_CBAR_INCHES_PER_TICK * n_ticks) / _MAP_CBAR_WIDTH
    return max(col_width, needed)


def _variable_label(da):
    return variable_label_for_display(da)


def _is_cftime_axis(values):
    import numpy as np

    arr = np.asarray(values)
    return (
        getattr(arr.dtype, "kind", None) == "O"
        and arr.size > 0
        and hasattr(arr.flat[0], "calendar")
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


def _format_lead(step_value):
    import numpy as np

    arr = np.asarray(step_value)
    if arr.dtype.kind != "m":
        return None
    days = int(arr.astype("timedelta64[D]").astype(int))
    return f"+{days}d"


def _format_column_title(t, bin_width_ns):
    """``YYYY-MM-DD``, or a left-edge range when median spacing is ≥ 2 days."""
    import numpy as np

    use_range = bin_width_ns is not None and bin_width_ns >= 2 * _NS_PER_DAY
    width = None
    if use_range:
        width = _dt.timedelta(microseconds=int(bin_width_ns // 1000))

    if hasattr(t, "calendar"):
        if width is None:
            return t.strftime("%Y-%m-%d")
        try:
            end = t + width - _dt.timedelta(days=1)
        except (TypeError, ValueError):
            return t.strftime("%Y-%m-%d")
        return f"{t.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"

    start = np.asarray(t).astype("datetime64[D]")
    start_s = np.datetime_as_string(start, unit="D")
    if width is None:
        return start_s
    end = (
        start.astype("datetime64[ns]")
        + np.timedelta64(int(bin_width_ns), "ns")
        - np.timedelta64(1, "D")
    ).astype("datetime64[D]")
    return f"{start_s} to {np.datetime_as_string(end, unit='D')}"


def realize_valid_times(ds):
    """Return ``(valid_times, steps_or_None, panel_dim)``.

    Classic forecast: scalar init ``time`` + timedelta ``step`` → ``init + step``.
    Observation / analysis cube: the ``time`` dim is used as-is.
    """
    import numpy as np

    if "step" in ds.dims:
        steps = np.asarray(ds["step"].values)
        if (
            steps.dtype.kind == "m"
            and "time" in ds.coords
            and "time" not in ds.dims
            and getattr(ds["time"], "ndim", 1) == 0
        ):
            init = np.asarray(ds["time"].values)
            if init.dtype.kind == "M":
                return (init + steps).astype("datetime64[ns]"), steps, "step"
            if init.dtype.kind == "O" and hasattr(np.asarray(init).reshape(-1)[0], "calendar"):
                init_t = np.asarray(init).reshape(-1)[0]
                realized = np.array(
                    [
                        init_t + _dt.timedelta(days=int(s.astype("timedelta64[D]").astype(int)))
                        for s in steps
                    ],
                    dtype=object,
                )
                return realized, steps, "step"
        if steps.dtype.kind == "m":
            raise UsageError(
                "step axis has no scalar init time to realize valid times; "
                "pass a forecast cube with a scalar time init, a time-dim "
                "observation cube, or run the step-to-time skill first"
            )
    try:
        tdim = pick_time_dim(ds, None)
    except UsageError:
        raise UsageError(
            f"each input needs a time dim or a step dim with a scalar init time; "
            f"got dims {list(ds.dims)}"
        ) from None
    vals = np.asarray(ds[tdim].values)
    if tdim == "step" and vals.dtype.kind == "m":
        raise UsageError(
            "step axis has no scalar init time to realize valid times; "
            "pass a forecast cube with a scalar time init, a time-dim "
            "observation cube, or run the step-to-time skill first"
        )
    steps = np.asarray(ds["step"].values) if tdim == "step" and "step" in ds.coords else None
    return vals, steps, tdim


def _encode_times(values):
    """Encode valid times to comparable scalars. Returns ``(enc, is_cftime, calendar)``."""
    import numpy as np

    arr = np.asarray(values)
    kind = _axis_kind(arr)
    if kind is None:
        raise DataError(f"valid times are not a datetime or timedelta axis (dtype={arr.dtype})")
    if kind == "timedelta":
        raise DataError(
            "valid times resolved to a timedelta axis; need calendar dates "
            "(init + step, or a time dim)"
        )
    if _is_cftime_axis(arr):
        import cftime

        calendar = arr.flat[0].calendar
        enc = np.asarray(
            cftime.date2num(arr, units="days since 1970-01-01", calendar=calendar),
            dtype="float64",
        )
        return enc, True, calendar
    enc = arr.astype("datetime64[ns]").astype("int64")
    return enc, False, None


def _median_spacing(enc):
    import numpy as np

    if enc.size < 2:
        return None
    return float(np.median(np.abs(np.diff(enc))))


def union_encoded(enc_rows, tol):
    """Sorted unique column encodings, clustering values within ``tol``."""
    import numpy as np

    all_vals = np.concatenate([np.asarray(row, dtype="float64") for row in enc_rows if len(row)])
    if all_vals.size == 0:
        return np.array([], dtype="float64")
    sorted_vals = np.sort(all_vals)
    columns = []
    cluster = [float(sorted_vals[0])]
    for v in sorted_vals[1:]:
        if abs(float(v) - cluster[0]) <= tol:
            cluster.append(float(v))
        else:
            columns.append(cluster[0])
            cluster = [float(v)]
    columns.append(cluster[0])
    return np.asarray(columns, dtype="float64")


def match_row(row_enc, col_enc, tol):
    """Index into ``row_enc`` for each column, or None when no match within ``tol``."""
    import numpy as np

    row_enc = np.asarray(row_enc, dtype="float64")
    matches = []
    if row_enc.size == 0:
        return [None] * len(col_enc)
    order = np.argsort(row_enc)
    sorted_enc = row_enc[order]
    for target in col_enc:
        idx = int(np.searchsorted(sorted_enc, target))
        best = None
        best_d = None
        for cand in (idx - 1, idx):
            if 0 <= cand < sorted_enc.size:
                d = abs(float(sorted_enc[cand]) - float(target))
                if d <= tol and (best_d is None or d < best_d):
                    best_d = d
                    best = int(order[cand])
        matches.append(best)
    return matches


def align_valid_times(datasets, panels=None):
    """Union valid-time columns and per-row matches.

    Returns ``(column_times, matches, bin_width_ns, steps_per_row, panel_dims)``.
    ``matches[row][col]`` is an integer index along that row's panel dim, or None.
    """
    import numpy as np

    realized = [realize_valid_times(ds) for ds in datasets]
    times_list = [t for t, _s, _d in realized]
    steps_per_row = [s for _t, s, _d in realized]
    panel_dims = [d for _t, _s, d in realized]

    kinds = [_axis_kind(t) for t in times_list]
    if any(k is None for k in kinds) or len(set(kinds)) != 1:
        raise DataError(
            "the inputs have different time resolutions "
            f"(dtypes={[np.asarray(t).dtype for t in times_list]}); "
            "aggregate both inputs to a common resolution first, e.g. with the "
            "aggregate-temporal skill."
        )
    cftime_flags = [_is_cftime_axis(t) for t in times_list]
    if any(cftime_flags) and not all(cftime_flags):
        raise DataError(
            "cannot compare a model-calendar (cftime) time axis against a "
            "standard-calendar (datetime64) time axis; convert both to a common "
            "calendar first with the convert-calendar skill."
        )
    calendars = []
    if all(cftime_flags):
        for t in times_list:
            calendars.append(np.asarray(t).flat[0].calendar)
        if len(set(calendars)) > 1:
            raise DataError(
                f"the inputs use different model calendars {calendars!r}; "
                "convert both to a common calendar first with the convert-calendar skill."
            )

    encoded = []
    is_cftime = False
    for t in times_list:
        enc, is_cftime, _cal = _encode_times(t)
        encoded.append(enc)

    widths = [_median_spacing(enc) for enc in encoded]
    known = [w for w in widths if w is not None]
    if len(known) >= 2:
        ref = max(known)
        for w in known:
            rel = abs(w - ref) / max(ref, 1.0)
            if rel > 1e-3:
                if is_cftime:
                    detail = ", ".join(f"{x:.4g} days" for x in known)
                else:
                    detail = ", ".join(f"{x:.0f} ns" for x in known)
                raise DataError(
                    "the inputs have different time resolutions "
                    f"(median bin widths {detail}); "
                    "aggregate both inputs to a common resolution first, e.g. with the "
                    "aggregate-temporal skill."
                )

    tol = 1.0 / 86400.0 if is_cftime else float(_TOL_NS)
    col_enc = union_encoded(encoded, tol)
    if col_enc.size == 0:
        raise DataError("no valid times on any input.")

    matches = [match_row(enc, col_enc, tol) for enc in encoded]
    shared = any(sum(m is not None for m in col) >= 2 for col in zip(*matches, strict=True))
    if not shared:
        raise DataError("no overlapping valid times between the inputs.")
    if panels is not None:
        col_enc = col_enc[: min(panels, col_enc.size)]
        matches = [row[: col_enc.size] for row in matches]

    # Representative raw time per column: first row that hits it.
    column_times = []
    for col, enc_val in enumerate(col_enc):
        raw = None
        for row, row_matches in enumerate(matches):
            idx = row_matches[col]
            if idx is not None:
                raw = times_list[row][idx]
                break
        if raw is None:
            if is_cftime:
                import cftime

                cal = calendars[0] if calendars else "standard"
                raw = cftime.num2date(enc_val, units="days since 1970-01-01", calendar=cal)
            else:
                raw = np.datetime64(int(enc_val), "ns")
        column_times.append(raw)

    bin_width_ns = None
    if not is_cftime and known:
        bin_width_ns = known[0]
    elif is_cftime and known:
        bin_width_ns = known[0] * _NS_PER_DAY

    return column_times, matches, bin_width_ns, steps_per_row, panel_dims


def _flatten_da(da, panel_dim, lat_dim, lon_dim):
    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)
    extras = [d for d in da.dims if d not in (panel_dim, lat_dim, lon_dim)]
    if extras:
        raise UsageError(
            f"dimension {extras[0]!r} remains after averaging ensemble members; "
            f"heatmap panels only {panel_dim!r} — select a position from "
            f"{extras[0]!r} with the select skill"
        )
    return da


def _slice_bbox_mask(da, lat_dim, lon_dim, bbox, polygon, label):
    import numpy as np
    import xarray as xr

    if bbox is None and polygon is None:
        return da
    da = ensure_normalized_longitude(da, lon_dim)
    if bbox is not None:
        r_n, r_w, r_s, r_e = bbox
        da = da.sel({lat_dim: lat_slice(da[lat_dim].values, r_n, r_s)})
        if r_w > r_e:
            da = da.where((da[lon_dim] >= r_w) | (da[lon_dim] <= r_e), drop=True)
        else:
            da = da.sel({lon_dim: slice(r_w, r_e)})
    if polygon is not None:
        import shapely

        lon_grid, lat_grid = np.meshgrid(da[lon_dim].values, da[lat_dim].values)
        mask = shapely.contains_xy(polygon, lon_grid, lat_grid)
        if not bool(mask.any()):
            print(
                f"Warning: --mask-geojson polygon does not intersect input '{label}'; "
                "its panels will be entirely empty.",
                file=sys.stderr,
            )
        da = da.where(xr.DataArray(mask, dims=(lat_dim, lon_dim)))
    if bbox is not None:
        r_n, r_w, r_s, r_e = bbox
        if r_w > r_e:
            da = da.assign_coords({lon_dim: ((da[lon_dim] - r_w) % 360.0) + r_w}).sortby(lon_dim)
    if da.sizes.get(lat_dim, 0) == 0 or da.sizes.get(lon_dim, 0) == 0:
        raise UsageError(
            f"selection produced an empty grid on input '{label}' "
            "(no cells remain after --bbox/--mask-geojson); nothing to plot."
        )
    return da


def _extent_from_da(da, lat_dim, lon_dim, bbox):
    import numpy as np

    if bbox is not None:
        r_n, r_w, r_s, r_e = bbox
        if r_w > r_e:
            return [float(r_w), float(r_e) + 360.0, float(r_s), float(r_n)]
        return [float(r_w), float(r_e), float(r_s), float(r_n)]
    lat_vals = np.asarray(da[lat_dim].values)
    lon_vals = np.asarray(da[lon_dim].values)
    dlat = float(np.abs(np.diff(np.sort(lat_vals))).mean()) if lat_vals.size > 1 else 0.0
    dlon = float(np.abs(np.diff(np.sort(lon_vals))).mean()) if lon_vals.size > 1 else 0.0
    lon_min = float(lon_vals.min()) - dlon / 2
    lon_max = float(lon_vals.max()) + dlon / 2
    lat_min = float(lat_vals.min()) - dlat / 2
    lat_max = float(lat_vals.max()) + dlat / 2
    # Half-cell padding on a wrapped global lon axis is a 360° span whose
    # endpoints are the same meridian; Cartopy then collapses to a sliver.
    if lon_max - lon_min >= 360.0 - 1e-6:
        lon_min, lon_max = -180.0, 180.0
    return [lon_min, lon_max, max(lat_min, -90.0), min(lat_max, 90.0)]


@weather_skill(
    name="plot-compare-forecasts",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), action="append", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--colormap",
    default=None,
    help=(
        "matplotlib colormap name, or comma-separated colors. "
        "Default: discrete CHIRPS-GEFS precip classes for precip variables, else viridis."
    ),
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--fontsize",
    type=int,
    default=14,
    help="Base font size for column titles, row labels, ticks, and colorbars (default 14).",
)
@weather_skill.argument(
    "--panels",
    type=int,
    default=None,
    help="Cap on columns (earliest N of the union). Default: all union columns.",
)
@weather_skill.argument(
    "--label",
    action="append",
    default=None,
    help="Row label for each --input, in order. Omit to infer from metadata.",
)
@weather_skill.argument(
    "--mask-geojson",
    default=None,
    help="GeoJSON polygon; gridded cells outside become NaN.",
)
def plot_compare_forecasts(
    ds,
    bbox,
    variable,
    colormap,
    title,
    fontsize,
    panels,
    label,
    mask_geojson,
    output,
    **kwargs,
):
    """Compare two or more gridded datasets as a heatmap grid PNG."""
    if len(ds) < 2:
        raise UsageError(f"expected at least two --input paths, got {len(ds)}")
    if panels is not None and panels < 1:
        raise UsageError(f"--panels must be >= 1, got {panels}")

    import matplotlib

    matplotlib.use("Agg")
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm

    variable = variable or auto_variable(ds[0])
    if variable is None:
        raise UsageError("no usable variable in the first input.")
    for idx, one in enumerate(ds):
        if variable not in one:
            raise UsageError(
                f"variable '{variable}' missing from input {idx + 1}. "
                f"Available: {list(one.data_vars)}"
            )
    label_slots = resolve_input_labels(label, len(ds))
    datasets = [
        precip_for_display(to_standard_units(one, variables=[variable]), variable) for one in ds
    ]
    labels = [
        slot or dataset_display_label(one, f"input {idx + 1}")
        for idx, (slot, one) in enumerate(zip(label_slots, datasets, strict=True))
    ]

    unit_vals = []
    seen_units = {}
    for idx, ds in enumerate(datasets):
        u = variable_units(ds[variable])
        if isinstance(u, str) and u.strip():
            unit_vals.append(u)
            seen_units[labels[idx]] = u.strip()
    if unit_vals and any(not units_equal(unit_vals[0], u) for u in unit_vals[1:]):
        detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
        print(
            f"Warning: variable '{variable}' has differing units across the "
            f"inputs ({detail}). The grid shares one color scale, so values in "
            f"different units are not directly comparable in this figure.",
            file=sys.stderr,
        )

    column_times, matches, bin_width_ns, steps_per_row, panel_dims = align_valid_times(
        datasets, panels=panels
    )
    nrows = len(datasets)
    ncols = len(column_times)

    das = []
    lat_dims = []
    lon_dims = []
    polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
    for idx, ds in enumerate(datasets):
        da = ds[variable]
        lat_dim = cf_dim(da, "latitude")
        lon_dim = cf_dim(da, "longitude")
        if lat_dim is None or lon_dim is None or lat_dim not in da.dims or lon_dim not in da.dims:
            raise UsageError(f"input {idx + 1} needs lat/lon as dimensions; got {list(da.dims)}")
        da = _flatten_da(da, panel_dims[idx], lat_dim, lon_dim)
        da = _slice_bbox_mask(da, lat_dim, lon_dim, bbox, polygon, labels[idx])
        das.append(da)
        lat_dims.append(lat_dim)
        lon_dims.append(lon_dim)

    wrap_lon = not (bbox is not None and bbox[1] > bbox[3])
    extent = _extent_from_da(das[0], lat_dims[0], lon_dims[0], bbox)

    cmap, norm = _heatmap_scale(das[0], colormap)
    if (
        colormap is None
        and getattr(cmap, "name", None) in ("chirps_total", "chirps_short")
        and any(_is_precip_anomaly(da) for da in das)
    ):
        cmap, norm = _precip_anomaly_scale()
    vmin = vmax = None
    if norm is None:
        present_min = []
        present_max = []
        for row, da in enumerate(das):
            pdim = panel_dims[row]
            for idx in matches[row]:
                if idx is None:
                    continue
                slab = da.isel({pdim: idx})
                present_min.append(float(slab.min(skipna=True).values))
                present_max.append(float(slab.max(skipna=True).values))
        if present_max:
            vmin = float(np.nanmin(present_min))
            vmax = float(np.nanmax(present_max))
            if vmax > 0 and vmin < 0:
                m = max(abs(vmax), abs(vmin))
                vmin, vmax = -m, m
            if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
                vmin, vmax = 0.0, 1.0
        else:
            vmin, vmax = 0.0, 1.0
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(
            _colorbar_figure_width(ncols, _colorbar_tick_count(norm)),
            max(2.8 * nrows, 4.0) + (0.6 if title else 0.0),
        ),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
        squeeze=False,
    )
    if title:
        fig.suptitle(title, fontsize=_scaled_fontsize(fontsize, 1.1))

    tick_fs = _scaled_fontsize(fontsize, 0.7)
    panel_title_fs = _scaled_fontsize(fontsize, 0.85)
    lead_fs = _scaled_fontsize(fontsize, 0.65)
    na_fs = _scaled_fontsize(fontsize, 0.9)
    cbar_label_fs = _scaled_fontsize(fontsize, 0.50, floor=8)
    cbar_tick_fs = _scaled_fontsize(fontsize, 0.36, floor=6)

    contour = None
    for row, da in enumerate(das):
        pdim = panel_dims[row]
        lat_dim = lat_dims[row]
        lon_dim = lon_dims[row]
        for col, t in enumerate(column_times):
            ax = axes[row][col]
            col_title = _format_column_title(t, bin_width_ns)
            idx = matches[row][col]
            lead = None
            if idx is not None and steps_per_row[row] is not None:
                lead = _format_lead(steps_per_row[row][idx])
            if row == 0:
                ax.set_title(col_title, fontsize=panel_title_fs)
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
            gl.xlabel_style = {"size": tick_fs}
            gl.ylabel_style = {"size": tick_fs}
            if col != 0:
                gl.left_labels = False
            if idx is None:
                ax.text(
                    0.5,
                    0.5,
                    "n/a",
                    transform=ax.transAxes,
                    ha="center",
                    va="center",
                    fontsize=na_fs,
                    color="0.4",
                )
            else:
                slab = da.isel({pdim: idx}).transpose(lat_dim, lon_dim)
                mesh = ax.pcolormesh(
                    slab[lon_dim],
                    slab[lat_dim],
                    slab.values,
                    cmap=cmap,
                    norm=norm,
                    vmin=vmin,
                    vmax=vmax,
                    transform=ccrs.PlateCarree(),
                )
                if contour is None:
                    contour = mesh
                if lead:
                    ax.text(
                        0.03,
                        0.97,
                        lead,
                        transform=ax.transAxes,
                        ha="left",
                        va="top",
                        fontsize=lead_fs,
                        color="0.2",
                    )
            ax.set_ylabel(_axis_label(labels[row]), fontsize=fontsize)

    if contour is not None:
        fig.subplots_adjust(
            left=0.08,
            right=0.98,
            bottom=0.20,
            top=0.90 if title else 0.96,
            hspace=0.42 if nrows > 1 else 0.12,
            wspace=0.18,
        )
        cbar_ax = fig.add_axes([_MAP_CBAR_LEFT, 0.04, _MAP_CBAR_WIDTH, _MAP_CBAR_HEIGHT])
        cbar = fig.colorbar(
            contour,
            cax=cbar_ax,
            orientation="horizontal",
            **_cbar_boundary_kwargs(norm, cmap),
        )
        cbar.set_label(_variable_label(das[0]), fontsize=cbar_label_fs)
        cbar.ax.tick_params(labelsize=cbar_tick_fs)
    else:
        fig.tight_layout()

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_compare_forecasts()
