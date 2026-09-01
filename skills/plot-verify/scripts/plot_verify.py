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
"""Lead-week verification as a grid of maps: obs, forecast, and verify metric."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.standard_utils import (
    ensure_normalized_longitude,
    lat_slice,
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
_SKILL_VERSION = "0.0.3"

_VERIFY_VARS = {
    "hits": "event_hit",
    "bias": "bias",
    "mae": "mae",
}

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
_ROW_FALLBACKS = ("Observation", "Forecast", "Verification")
_METRIC_ROW_LABELS = {"hits": "Hits", "bias": "Bias", "mae": "MAE"}
_DATE_TAIL = re.compile(r"[_-]\d{4}-\d{2}-\d{2}(?:[_-]\d{2}-\d{2}-\d{2})?$")
_VAR_TAIL = re.compile(r"[_-](?:precip|tp)$", re.I)
_SOURCE_LABELS = {
    "arco-era5": "ERA5",
    "chirps": "CHIRPS",
    "cmip6": "CMIP6",
    "ecmwf-s2s": "ECMWF S2S",
    "ecmwf_s2s": "ECMWF S2S",
    "gefs": "GEFS",
    "ghcn-daily": "GHCN-Daily",
    "imerg": "IMERG",
    "kenya-forecasting-data": "Kenya forecast",
    "oisst": "OISST",
    "openaq": "OpenAQ",
    "smap": "SMAP",
    "tahmo": "TAHMO",
}
_FETCH_SKILL_LABELS = {
    "arco-era5-fetch": "ERA5",
    "chirps-fetch": "CHIRPS",
    "cmip6-fetch": "CMIP6",
    "dynamical-fetch": "Dynamical",
    "ecmwf-fetch": "ECMWF S2S",
    "ghcn-daily-fetch": "GHCN-Daily",
    "imerg-fetch": "IMERG",
    "kenya-forecast-fetch": "Kenya forecast",
    "oisst-fetch": "OISST",
    "openaq-fetch": "OpenAQ",
    "smap-fetch": "SMAP",
    "tahmo-fetch": "TAHMO",
}


def _prettify_token(text: str) -> str:
    key = text.lower().replace("_", "-")
    for src_key, display in sorted(_SOURCE_LABELS.items(), key=lambda item: -len(item[0])):
        sk = src_key.replace("_", "-")
        if key == sk or key.startswith(f"{sk}-"):
            return display
    parts = [p for p in re.split(r"[_-]+", text.strip()) if p]
    if not parts:
        return text
    out = []
    for part in parts:
        low = part.lower()
        if low in {"ecmwf", "s2s", "ghcn", "cmip6", "gefs", "oisst", "imerg", "smap"}:
            out.append(part.upper())
        elif low == "chirps":
            out.append("CHIRPS")
        else:
            out.append(part.capitalize())
    return " ".join(out)


def _normalize_stem(stem: str) -> str:
    base = _DATE_TAIL.sub("", stem)
    return _VAR_TAIL.sub("", base)


def _label_from_history(ds) -> str | None:
    import json

    raw = ds.attrs.get("weather_skills_history")
    if not raw:
        return None
    try:
        history = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not history or not isinstance(history[0], dict):
        return None
    skill = history[0].get("skill")
    if not isinstance(skill, str) or not skill.strip():
        return None
    skill = skill.strip()
    if skill in _FETCH_SKILL_LABELS:
        return _FETCH_SKILL_LABELS[skill]
    if skill.endswith("-fetch"):
        return _prettify_token(skill[: -len("-fetch")])
    return _prettify_token(skill)


def _label_from_source_token(token: str) -> str:
    text = token.strip()
    if not text:
        return ""
    if ":" in text and "/" not in text and "\\" not in text and not text.endswith(".zarr"):
        head, _, tail = text.partition(":")
        if head in _SOURCE_LABELS:
            return _SOURCE_LABELS[head]
        if head in _FETCH_SKILL_LABELS:
            return _FETCH_SKILL_LABELS[head]
        return _prettify_token(tail or head)
    if "/" in text or "\\" in text or text.endswith(".zarr"):
        text = Path(text).stem
    return _prettify_token(_normalize_stem(text))


def _row_product_label(ds, fallback: str) -> str:
    """Short product name for row titles — not per-init filenames."""
    label = _label_from_history(ds)
    if label:
        return label
    src = ds.attrs.get("weather_skills_source")
    if isinstance(src, str) and src.strip():
        label = _label_from_source_token(src)
        if label:
            return label
    enc = ds.encoding.get("source")
    if isinstance(enc, str) and enc.strip():
        label = _label_from_source_token(enc)
        if label:
            return label
    return fallback


def _metric_from_verify(ds, role: str) -> str:
    metric = ds.attrs.get("verify_metric")
    if metric not in _VERIFY_VARS:
        raise UsageError(
            f"{role} is missing a supported verify_metric attr "
            f"({list(_VERIFY_VARS)}); run the verify skill first."
        )
    return metric


def _verify_field(ds, metric: str, role: str):
    name = _VERIFY_VARS[metric]
    if name not in ds:
        raise UsageError(f"{role} missing verification variable {name!r}.")
    return ds[name]


def _row_labels(obs, forecasts, metric="hits"):
    """Y-axis product names: one short label per row, not per --forecast file."""
    obs_label = _row_product_label(obs, _ROW_FALLBACKS[0])
    fc_names = [_row_product_label(ds, _ROW_FALLBACKS[1]) for ds in forecasts]
    unique = list(dict.fromkeys(fc_names))
    forecast_label = unique[0] if len(unique) == 1 else " / ".join(unique)
    verify_label = _METRIC_ROW_LABELS.get(metric, _ROW_FALLBACKS[2])
    return (obs_label, forecast_label, verify_label)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _dim(ds, *names: str) -> str | None:
    return next((n for n in names if n in ds.dims), None)


def _median_spacing(coord) -> float | None:
    import numpy as np

    vals = coord.values
    if getattr(vals, "size", 0) < 2:
        return None
    return float(np.median(np.abs(np.diff(np.asarray(vals, dtype=float)))))


def _require_same_grid(left, right, left_role, right_role) -> None:
    """Refuse when lat/lon spacing differs (coarsen obs onto the forecast, not the reverse)."""
    import numpy as np

    lat_a = _dim(left, "latitude", "lat")
    lon_a = _dim(left, "longitude", "lon")
    lat_b = _dim(right, "latitude", "lat")
    lon_b = _dim(right, "longitude", "lon")
    if not all((lat_a, lon_a, lat_b, lon_b)):
        return
    pairs = (
        (_median_spacing(left[lat_a]), _median_spacing(right[lat_b]), "latitude"),
        (_median_spacing(left[lon_a]), _median_spacing(right[lon_b]), "longitude"),
    )
    mismatched = [
        axis
        for d_a, d_b, axis in pairs
        if d_a is not None and d_b is not None and not np.isclose(d_a, d_b, rtol=0.01, atol=1e-6)
    ]
    if mismatched:
        raise UsageError(
            f"{right_role} grid spacing does not match {left_role} on "
            f"{' and '.join(mismatched)}; coarsen --obs onto the forecast "
            "lat/lon resolution (and offset) before plot-verify. Do not "
            "downscale the forecast to the obs grid."
        )


def _parse_colormap(spec):
    if spec is None or "," not in spec:
        return spec
    from matplotlib.colors import LinearSegmentedColormap

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    return LinearSegmentedColormap.from_list("custom", parts)


def _precip_scale():
    from matplotlib.colors import BoundaryNorm, ListedColormap

    cmap = ListedColormap(PRECIP_COLORS, name="wgbrp")
    return cmap, BoundaryNorm(PRECIP_BOUNDS, ncolors=cmap.N, clip=True)


def _heatmap_scale(da, colormap):
    if colormap:
        return _parse_colormap(colormap), None
    kind = classify_variable(
        da.name or "",
        units=variable_units(da),
        standard_name=da.attrs.get("standard_name"),
    )
    if kind in ("precip", "precip_amount"):
        return _precip_scale()
    return "viridis", None


def _variable_label(da):
    name = da.attrs.get("long_name") or da.attrs.get("GRIB_name") or da.name or "value"
    units = format_units_for_display(variable_units(da) or da.attrs.get("units"))
    if units:
        return f"{name} [{units}]"
    return str(name)


def _hits_scale():
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap

    values = np.array([-1.0, 0.0, 1.0])
    colors = ["#d73027", "#f0f0f0", "#1a9850"]
    mids = (values[:-1] + values[1:]) / 2.0
    bounds = np.concatenate(([values[0] - 0.5], mids, [values[-1] + 0.5]))
    cmap = ListedColormap(colors)
    return cmap, BoundaryNorm(bounds, cmap.N), ["disagree", "below", "hit"]


def _error_scale(da, metric):
    """Colormap and optional norm for bias/mae verification row."""
    import numpy as np

    vmin = float(da.min(skipna=True).values)
    vmax = float(da.max(skipna=True).values)
    if not np.isfinite(vmin) or not np.isfinite(vmax):
        return "viridis", None, 0.0, 1.0
    if metric == "bias":
        if vmax > 0 and vmin < 0:
            m = max(abs(vmax), abs(vmin))
            vmin, vmax = -m, m
        elif vmin == vmax:
            vmin, vmax = vmin - 1.0, vmax + 1.0
        return "RdBu_r", None, vmin, vmax
    if vmin == vmax:
        vmin, vmax = 0.0, max(vmax, 1.0)
    elif vmin < 0:
        vmin = 0.0
    return "viridis", None, vmin, vmax


def _lat_lon(da, role):
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if lat_dim is None or lon_dim is None or lat_dim not in da.dims or lon_dim not in da.dims:
        raise UsageError(f"{role} needs lat/lon as dimensions; got {list(da.dims)}")
    return lat_dim, lon_dim


def _squeeze_map(da, role):
    """Reduce to a single lat/lon field (the verifying week)."""
    if "number" in da.dims:
        da = da.mean("number", keep_attrs=True)
    if "step" in da.dims and "time" not in da.dims:
        raise UsageError(
            f"{role} still has a step axis; run step-to-time and select the "
            "verifying week before plot-verify so valid times align with --obs."
        )
    lat_dim, lon_dim = _lat_lon(da, role)
    extras = [d for d in da.dims if d not in (lat_dim, lon_dim)]
    for dim in extras:
        if da.sizes[dim] != 1:
            raise UsageError(
                f"{role} has {dim} size {da.sizes[dim]}; select the verifying "
                "week (one time) before plot-verify."
            )
        da = da.squeeze(dim, drop=True)
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
                f"Warning: --mask-geojson polygon does not intersect {label}; "
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
            f"selection produced an empty grid on {label} "
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


def _pick_variable(ds, variable, role):
    name = variable or auto_variable(ds)
    if not name or name not in ds:
        raise UsageError(f"variable {name!r} missing from {role}. Available: {list(ds.data_vars)}")
    return name


def _prepare(ds, variable):
    return precip_for_display(to_standard_units(ds, variables=[variable]), variable)


@weather_skill(
    name="plot-verify",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--obs", type=Dataset("spatial"), required=True)
@weather_skill.argument(
    "--forecast",
    type=Dataset("spatial"),
    action="append",
    required=True,
)
@weather_skill.argument(
    "--verify",
    type=Dataset("any"),
    action="append",
    required=True,
    help="Verify Zarr from the verify skill, once per --forecast (same order).",
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--lead",
    action="append",
    default=None,
    help="Column label, once per --forecast. Default: Week N … Week 1 (least recent to most recent).",
)
@weather_skill.argument(
    "--colormap",
    default=None,
    help=(
        "matplotlib colormap name, or comma-separated colors, for obs/forecast rows. "
        "Default: discrete Kenya/S2S precip classes for precip, else viridis."
    ),
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--mask-geojson",
    default=None,
    help="GeoJSON polygon; gridded cells outside become NaN.",
)
def plot_verify(
    obs,
    forecast,
    verify,
    bbox,
    variable,
    lead,
    colormap,
    title,
    mask_geojson,
    output,
    **kwargs,
):
    """Lead-week verification grid from obs, forecast, and pre-computed verify Zarrs."""
    forecasts = _as_list(forecast)
    verify_sets = _as_list(verify)
    if not forecasts:
        raise UsageError("expected at least one --forecast.")
    if len(verify_sets) != len(forecasts):
        raise UsageError(
            f"--verify was passed {len(verify_sets)} time(s) but --forecast was passed "
            f"{len(forecasts)} time(s); pass one --verify per --forecast."
        )
    leads = _as_list(lead)
    if leads and len(leads) != len(forecasts):
        raise UsageError(
            f"--lead was passed {len(leads)} time(s) but --forecast was passed "
            f"{len(forecasts)} time(s); pass one --lead per --forecast."
        )
    if not leads:
        leads = [f"Week {i}" for i in range(len(forecasts), 0, -1)]

    metrics = [_metric_from_verify(ds, f"--verify {i + 1}") for i, ds in enumerate(verify_sets)]
    if len(set(metrics)) != 1:
        raise UsageError(
            f"all --verify inputs must share the same verify_metric; got {metrics}."
        )
    metric = metrics[0]
    row_labels = _row_labels(obs, forecasts, metric)

    import matplotlib

    matplotlib.use("Agg")
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm

    obs_name = _pick_variable(obs, variable, "--obs")
    fc_names = [
        _pick_variable(fc, variable, f"--forecast {i + 1}") for i, fc in enumerate(forecasts)
    ]
    obs_ds = _prepare(obs, obs_name)
    fc_datasets = [_prepare(fc, name) for fc, name in zip(forecasts, fc_names, strict=True)]

    u_obs = variable_units(obs_ds[obs_name])
    for i, (fc_ds, fc_name) in enumerate(zip(fc_datasets, fc_names, strict=True)):
        u_fc = variable_units(fc_ds[fc_name])
        if (
            isinstance(u_fc, str)
            and u_fc.strip()
            and isinstance(u_obs, str)
            and u_obs.strip()
            and not units_equal(u_fc, u_obs)
        ):
            print(
                f"Warning: --forecast {i + 1} {fc_name!r} units={u_fc.strip()!r} and "
                f"--obs {obs_name!r} units={u_obs.strip()!r} differ.",
                file=sys.stderr,
            )

    _require_same_grid(fc_datasets[0], obs_ds, "--forecast 1", "--obs")
    for i, fc_ds in enumerate(fc_datasets[1:], start=2):
        _require_same_grid(fc_datasets[0], fc_ds, "--forecast 1", f"--forecast {i}")

    polygon = polygon_from_geojson(mask_geojson) if mask_geojson else None
    obs_da = _squeeze_map(obs_ds[obs_name], "--obs")
    obs_lat, obs_lon = _lat_lon(obs_da, "--obs")
    obs_da = _slice_bbox_mask(obs_da, obs_lat, obs_lon, bbox, polygon, "--obs")

    columns = []
    for i, (fc_ds, fc_name, verify_ds, label) in enumerate(
        zip(fc_datasets, fc_names, verify_sets, leads, strict=True), start=1
    ):
        role = f"--forecast {i} ({label})"
        fc_da = _squeeze_map(fc_ds[fc_name], role)
        lat_dim, lon_dim = _lat_lon(fc_da, role)
        fc_da = _slice_bbox_mask(fc_da, lat_dim, lon_dim, bbox, polygon, role)
        verify_da = _squeeze_map(
            _verify_field(verify_ds, metric, f"--verify {i}"),
            f"--verify {i}",
        )
        verify_da = _slice_bbox_mask(verify_da, lat_dim, lon_dim, bbox, polygon, f"--verify {i}")
        summary = verify_ds.attrs.get("verify_score_summary")
        if isinstance(summary, str) and summary.strip():
            print(f"{label}  {summary.strip()}")
        columns.append((label, fc_da, verify_da, lat_dim, lon_dim))

    wrap_lon = not (bbox is not None and bbox[1] > bbox[3])
    extent = _extent_from_da(obs_da, obs_lat, obs_lon, bbox)
    cmap, norm = _heatmap_scale(obs_da, colormap)
    verify_cmap = verify_norm = verify_vmin = verify_vmax = verify_labels = None
    if metric == "hits":
        verify_cmap, verify_norm, verify_labels = _hits_scale()
    else:
        all_verify = [col[2] for col in columns]
        import xarray as xr

        stacked = xr.concat(all_verify, dim="panel")
        verify_cmap, verify_norm, verify_vmin, verify_vmax = _error_scale(stacked, metric)
    vmin = vmax = None
    if norm is None:
        present = [float(obs_da.min(skipna=True).values), float(obs_da.max(skipna=True).values)]
        for _label, fc_da, *_rest in columns:
            present.append(float(fc_da.min(skipna=True).values))
            present.append(float(fc_da.max(skipna=True).values))
        vmin = float(np.nanmin(present))
        vmax = float(np.nanmax(present))
        if vmax > 0 and vmin < 0:
            m = max(abs(vmax), abs(vmin))
            vmin, vmax = -m, m
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
            vmin, vmax = 0.0, 1.0

    nrows, ncols = 3, len(columns)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(max(3.2 * ncols, 6.0), max(2.8 * nrows, 5.0) + (0.6 if title else 0.0)),
        sharex=True,
        sharey=True,
        subplot_kw={"projection": ccrs.PlateCarree()},
        squeeze=False,
    )
    if title:
        fig.suptitle(title)

    def _draw(ax, da, lat_dim, lon_dim, this_cmap, this_norm, this_vmin, this_vmax, *, left_labels):
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
        if not left_labels:
            gl.left_labels = False
        slab = da.transpose(lat_dim, lon_dim)
        return ax.pcolormesh(
            slab[lon_dim],
            slab[lat_dim],
            slab.values,
            cmap=this_cmap,
            norm=this_norm,
            vmin=this_vmin,
            vmax=this_vmax,
            transform=ccrs.PlateCarree(),
        )

    field_mesh = verify_mesh = None
    for col, (label, fc_da, verify_da, lat_dim, lon_dim) in enumerate(columns):
        left = col == 0
        axes[0][col].set_title(label, fontsize=9)
        mesh = _draw(
            axes[0][col], obs_da, obs_lat, obs_lon, cmap, norm, vmin, vmax, left_labels=left
        )
        if field_mesh is None:
            field_mesh = mesh
        mesh = _draw(
            axes[1][col], fc_da, lat_dim, lon_dim, cmap, norm, vmin, vmax, left_labels=left
        )
        if field_mesh is None:
            field_mesh = mesh
        if metric == "hits":
            mesh = _draw(
                axes[2][col],
                verify_da,
                lat_dim,
                lon_dim,
                verify_cmap,
                verify_norm,
                None,
                None,
                left_labels=left,
            )
        else:
            mesh = _draw(
                axes[2][col],
                verify_da,
                lat_dim,
                lon_dim,
                verify_cmap,
                verify_norm,
                verify_vmin,
                verify_vmax,
                left_labels=left,
            )
        if verify_mesh is None:
            verify_mesh = mesh

    fig.tight_layout(rect=[0.12, 0.10, 1, 0.94 if title else 0.98])
    for row, row_label in enumerate(row_labels):
        pos = axes[row][0].get_position()
        fig.text(
            pos.x0 - 0.04,
            (pos.y0 + pos.y1) / 2,
            row_label,
            rotation=90,
            va="center",
            ha="right",
            fontsize=9,
        )
    if field_mesh is not None:
        cbar_ax = fig.add_axes([0.08, 0.055, 0.50, 0.02])
        cbar_kw = {}
        if isinstance(norm, BoundaryNorm):
            cbar_kw["spacing"] = "uniform"
            cbar_kw["ticks"] = list(norm.boundaries)
        cbar = fig.colorbar(field_mesh, cax=cbar_ax, orientation="horizontal", **cbar_kw)
        cbar.set_label(_variable_label(obs_da))
    if verify_mesh is not None:
        verify_ax = fig.add_axes([0.66, 0.055, 0.26, 0.02])
        if metric == "hits":
            verify_cbar = fig.colorbar(
                verify_mesh, cax=verify_ax, orientation="horizontal", ticks=[-1, 0, 1]
            )
            verify_cbar.set_ticklabels(verify_labels)
            verify_cbar.set_label("event")
        else:
            verify_cbar = fig.colorbar(verify_mesh, cax=verify_ax, orientation="horizontal")
            units = format_units_for_display(u_obs)
            label = _METRIC_ROW_LABELS[metric]
            verify_cbar.set_label(f"{label} [{units}]" if units else label)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_verify()
