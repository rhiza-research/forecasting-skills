# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
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
from weather_skills_core.standard_utils import (
    dataset_label,
    lat_slice,
    pick_time_dim,
    polygon_from_geojson,
)
from weather_skills_core.units import (
    classify_variable,
    format_units_for_display,
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# ECMWF-S2S4AFRICA / Kenya product palette (same as plot).
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

_NS_PER_DAY = 86_400_000_000_000
_TOL_NS = 1_000_000_000  # 1 s, matching plot-compare


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
        units=variable_units(da),
        standard_name=da.attrs.get("standard_name"),
    )
    if kind in ("precip", "precip_amount"):
        return _precip_colormap()
    return "viridis"


def _variable_label(da):
    label = da.attrs.get("GRIB_name") or da.attrs.get("long_name") or da.name or "value"
    units = format_units_for_display(variable_units(da))
    if units:
        return f"{label} [{units}]"
    return label


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
    """``YYYY-MM-DD``, or a right-edge range when median spacing is ≥ 2 days."""
    import numpy as np

    use_range = bin_width_ns is not None and bin_width_ns >= 2 * _NS_PER_DAY
    width = None
    if use_range:
        width = _dt.timedelta(microseconds=int(bin_width_ns // 1000))

    if hasattr(t, "calendar"):
        if width is None:
            return t.strftime("%Y-%m-%d")
        try:
            start = t - width + _dt.timedelta(days=1)
        except (TypeError, ValueError):
            return t.strftime("%Y-%m-%d")
        return f"{start.strftime('%Y-%m-%d')} to {t.strftime('%Y-%m-%d')}"

    end = np.asarray(t).astype("datetime64[D]")
    end_s = np.datetime_as_string(end, unit="D")
    if width is None:
        return end_s
    start = (
        end.astype("datetime64[ns]")
        - np.timedelta64(int(bin_width_ns), "ns")
        + np.timedelta64(1, "D")
    ).astype("datetime64[D]")
    return f"{np.datetime_as_string(start, unit='D')} to {end_s}"


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
    lon_vals = np.asarray(da[lon_dim].values)
    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
        da = da.assign_coords({lon_dim: ((da[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)
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
    return [
        float(lon_vals.min()) - dlon / 2,
        float(lon_vals.max()) + dlon / 2,
        float(lat_vals.min()) - dlat / 2,
        float(lat_vals.max()) + dlat / 2,
    ]


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
        "Default: Kenya/S2S precip palette for precip variables, else viridis."
    ),
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--panels",
    type=int,
    default=None,
    help="Cap on columns (earliest N of the union). Default: all union columns.",
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
    panels,
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

    variable = variable or auto_variable(ds[0])
    if variable is None:
        raise UsageError("no usable variable in the first input.")
    for idx, one in enumerate(ds):
        if variable not in one:
            raise UsageError(
                f"variable '{variable}' missing from input {idx + 1}. "
                f"Available: {list(one.data_vars)}"
            )
    datasets = [
        precip_for_display(to_standard_units(one, variables=[variable]), variable) for one in ds
    ]
    labels = [dataset_label(ds, f"input {idx + 1}") for idx, ds in enumerate(datasets)]

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

    cmap = _heatmap_cmap(das[0], colormap)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(max(3.2 * ncols, 6.0), max(2.8 * nrows, 4.0) + (0.6 if title else 0.0)),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
        squeeze=False,
    )
    if title:
        fig.suptitle(title)

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
                ax.set_title(col_title, fontsize=9)
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
                    fontsize=12,
                    color="0.4",
                )
            else:
                slab = da.isel({pdim: idx}).transpose(lat_dim, lon_dim)
                mesh = ax.pcolormesh(
                    slab[lon_dim],
                    slab[lat_dim],
                    slab.values,
                    cmap=cmap,
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
                        fontsize=8,
                        color="0.2",
                    )
            if col == 0:
                ax.set_ylabel(labels[row])

    if contour is not None:
        fig.tight_layout(rect=[0, 0.06, 1, 0.94 if title else 0.98])
        cbar_ax = fig.add_axes([0.15, 0.02, 0.7, 0.02])
        cbar = fig.colorbar(contour, cax=cbar_ax, orientation="horizontal")
        cbar.set_label(_variable_label(das[0]))
    else:
        fig.tight_layout()

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_compare_forecasts()
