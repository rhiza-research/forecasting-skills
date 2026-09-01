"""Forecast-vs-observation verification metrics (hits, bias, MAE)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from weather_skills_core.errors import UsageError
from weather_skills_core.standard_utils import latitude_weights

Metric = Literal["hits", "bias", "mae"]
METRICS: tuple[Metric, ...] = ("hits", "bias", "mae")

_VAR_NAMES: dict[Metric, str] = {
    "hits": "event_hit",
    "bias": "bias",
    "mae": "mae",
}


def _dim(obj, *names: str) -> str | None:
    dims = obj.dims if hasattr(obj, "dims") else obj
    return next((n for n in names if n in dims), None)


def _median_spacing(coord) -> float | None:
    import numpy as np

    vals = coord.values
    if getattr(vals, "size", 0) < 2:
        return None
    return float(np.median(np.abs(np.diff(np.asarray(vals, dtype=float)))))


def require_obs_on_forecast_grid(forecast, obs) -> None:
    """Obs must already be at the forecast's lat/lon spacing (coarsen obs, not the forecast)."""
    import numpy as np

    lat_fc = _dim(forecast, "latitude", "lat")
    lon_fc = _dim(forecast, "longitude", "lon")
    lat_obs = _dim(obs, "latitude", "lat")
    lon_obs = _dim(obs, "longitude", "lon")
    if not all((lat_fc, lon_fc, lat_obs, lon_obs)):
        return
    pairs = (
        (_median_spacing(forecast[lat_fc]), _median_spacing(obs[lat_obs]), "latitude"),
        (_median_spacing(forecast[lon_fc]), _median_spacing(obs[lon_obs]), "longitude"),
    )
    mismatched = [
        axis
        for d_fc, d_obs, axis in pairs
        if d_fc is not None
        and d_obs is not None
        and not np.isclose(d_fc, d_obs, rtol=0.01, atol=1e-6)
    ]
    if mismatched:
        raise UsageError(
            "obs grid spacing does not match the forecast on "
            f"{' and '.join(mismatched)}; coarsen --obs onto the forecast "
            "lat/lon resolution (and offset) before verify. Do not "
            "downscale the forecast to the obs grid."
        )


def _dequant(da):
    if getattr(getattr(da, "pint", None), "units", None) is not None:
        return da.pint.dequantify()
    return da


def _prepare_pair(fc, truth):
    """Dequantify, ensemble-mean, and inner-join align two fields."""
    import xarray as xr

    fc = _dequant(fc)
    truth = _dequant(truth)
    if "number" in fc.dims:
        fc = fc.mean("number", keep_attrs=True)
    if "number" in truth.dims:
        truth = truth.mean("number", keep_attrs=True)
    fc, truth = xr.align(fc, truth, join="inner")
    if any(size == 0 for size in fc.sizes.values()):
        raise UsageError(
            "no overlapping coordinates between --forecast and --obs; "
            "coarsen --obs onto the forecast grid (match obs to the forecast "
            "resolution, not the reverse) and align time "
            "(step-to-time / aggregate-temporal) first."
        )
    return fc, truth


@dataclass
class VerificationResult:
    field: object  # xr.DataArray
    obs_event: object | None = None
    metric: Metric = "hits"


def compute(fc, obs, *, metric: Metric = "hits", threshold: float = 1.0) -> VerificationResult:
    """Compute a verification field from aligned forecast and obs DataArrays."""
    import numpy as np
    import xarray as xr

    if metric not in METRICS:
        raise UsageError(f"metric must be one of {METRICS}; got {metric!r}.")

    fc, truth = _prepare_pair(fc, obs)
    var_name = _VAR_NAMES[metric]

    if metric == "hits":
        fc_event = fc >= threshold
        obs_event = truth >= threshold
        field = xr.where(
            fc_event & obs_event,
            1,
            xr.where(fc_event != obs_event, -1, 0),
        ).astype("float32")
        field = field.where(fc.notnull() & truth.notnull())
        field.name = var_name
        return VerificationResult(field=field, obs_event=obs_event, metric=metric)

    if metric == "bias":
        field = (fc - truth).astype("float32")
    else:
        field = np.abs(fc - truth).astype("float32")
    field = field.where(fc.notnull() & truth.notnull())
    field.name = var_name
    return VerificationResult(field=field, obs_event=None, metric=metric)


def field_attrs(
    metric: Metric,
    *,
    threshold: float,
    fc_name: str,
    obs_name: str,
    source_attrs: dict | None = None,
) -> dict:
    """CF-oriented attrs for a verification output variable."""
    import numpy as np

    var_key = fc_name if fc_name == obs_name else f"{fc_name},{obs_name}"
    attrs = dict(source_attrs or {})
    for key in ("standard_name", "GRIB_name", "GRIB_paramId"):
        attrs.pop(key, None)
    attrs["verify_metric"] = metric
    if metric == "hits":
        attrs.update(
            {
                "long_name": "Event verification",
                "units": "1",
                "flag_values": np.array([-1, 0, 1], dtype=np.int8),
                "flag_meanings": "disagree below hit",
                "event_threshold": threshold,
                "event_variable": var_key,
            }
        )
    elif metric == "bias":
        attrs.update(
            {
                "long_name": "Forecast bias (forecast − observation)",
            }
        )
    else:
        attrs.update(
            {
                "long_name": "Mean absolute error",
            }
        )
    return attrs


def hit_rate(classified, obs_event):
    """POD: hits / obs-events among finite cells. ``(rate_or_None, n_hit, n_obs)``."""
    finite = classified.notnull()
    n_obs = int(obs_event.where(finite, False).sum())
    n_hit = int(((classified == 1) & finite).sum())
    if n_obs == 0:
        return None, n_hit, n_obs
    return n_hit / n_obs, n_hit, n_obs


def regional_mean(da, lat_dim: str) -> float | None:
    """Cosine-latitude weighted spatial mean; None when no finite cells."""
    import numpy as np

    weights = latitude_weights(da[lat_dim]).broadcast_like(da)
    finite = da.notnull()
    if not bool(finite.any()):
        return None
    num = (da * weights).where(finite).sum(skipna=True)
    den = weights.where(finite).sum(skipna=True)
    if den == 0 or not np.isfinite(float(den)):
        return None
    return float(num / den)


def lat_dim_for(da) -> str | None:
    return _dim(da, "latitude", "lat")


def score_summary(
    metric: Metric,
    *,
    field,
    obs_event=None,
    units: str | None = None,
) -> str:
    """Regional score text (no column label) for stamping on verify output."""
    from weather_skills_core.units import format_units_for_display

    u = format_units_for_display(units) if units else ""
    u_suffix = f" {u}" if u else ""

    if metric == "hits":
        rate, n_hit, n_obs = hit_rate(field, obs_event)
        if rate is None:
            return "hit rate n/a  (0 obs events)"
        return f"hit rate {100 * rate:.0f}%  ({n_hit}/{n_obs} obs events)"

    lat = lat_dim_for(field)
    if lat is None:
        return f"{metric} n/a  (no latitude dim)"
    value = regional_mean(field, lat)
    if value is None:
        return f"{metric} n/a  (no finite cells)"

    if metric == "bias":
        sign = "+" if value >= 0 else ""
        return f"bias {sign}{value:.2g}{u_suffix}  (cos-lat mean)"
    return f"MAE {value:.2g}{u_suffix}  (cos-lat mean)"


def format_score(
    label: str,
    metric: Metric,
    *,
    field,
    obs_event=None,
    units: str | None = None,
) -> str:
    """One stdout summary line for a verification column."""
    body = score_summary(metric, field=field, obs_event=obs_event, units=units)
    return f"{label}  {body}"


def verify_variable(metric: Metric) -> str:
    return _VAR_NAMES[metric]
