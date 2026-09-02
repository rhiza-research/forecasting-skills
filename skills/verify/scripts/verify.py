# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Forecast vs observation verification: hits, bias, or MAE."""

import sys

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.standard_utils import latitude_weights
from weather_skills_core.units import (
    format_units_for_display,
    units_equal,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_METRICS = ("hits", "bias", "mae")
_VAR = {"hits": "event_hit", "bias": "bias", "mae": "mae"}


@weather_skill(
    name="verify",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--forecast", type=Dataset("any"), required=True)
@weather_skill.argument("--obs", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--metric",
    choices=list(_METRICS),
    default="hits",
    help="Verification metric: hits (event classification), bias (forecast − obs), or mae.",
)
@weather_skill.argument(
    "--threshold",
    type=float,
    default=1.0,
    help="Event cutoff for --metric hits: a cell is an event when the variable is >= this.",
)
def verify(forecast, obs, variable, metric, threshold, **kwargs):
    """Forecast vs observation verification: hits, bias, or MAE."""
    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, ds, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(ds.data_vars)}"
            )

    fc, truth = forecast[fc_name], obs[obs_name]
    if "step" in fc.dims and "time" not in fc.dims and "time" in truth.dims:
        raise UsageError(
            "forecast still has a step axis; run step-to-time before verify "
            "so valid times can align with --obs."
        )

    mismatched = []
    for axis, names in (("latitude", ("latitude", "lat")), ("longitude", ("longitude", "lon"))):
        a = next((n for n in names if n in forecast.dims), None)
        b = next((n for n in names if n in obs.dims), None)
        if not a or not b:
            continue
        fa = np.asarray(forecast[a].values, dtype=float)
        fb = np.asarray(obs[b].values, dtype=float)
        if fa.size < 2 or fb.size < 2:
            continue
        if not np.isclose(
            float(np.median(np.abs(np.diff(fa)))),
            float(np.median(np.abs(np.diff(fb)))),
            rtol=0.01,
            atol=1e-6,
        ):
            mismatched.append(axis)
    if mismatched:
        raise UsageError(
            "obs grid spacing does not match the forecast on "
            f"{' and '.join(mismatched)}; coarsen --obs onto the forecast "
            "with --reference-grid <forecast.zarr>. Do not "
            "downscale the forecast to the obs grid."
        )

    pair = []
    for da in (fc, truth):
        if getattr(getattr(da, "pint", None), "units", None) is not None:
            da = da.pint.dequantify()
        if "number" in da.dims:
            da = da.mean("number", keep_attrs=True)
        pair.append(da)
    fc, truth = xr.align(*pair, join="inner")
    if any(size == 0 for size in fc.sizes.values()):
        raise UsageError(
            "no overlapping coordinates between --forecast and --obs; "
            "coarsen --obs --reference-grid <forecast.zarr> and align time "
            "(step-to-time / aggregate-temporal) first."
        )

    u_fc = variable_units(forecast[fc_name])
    u_obs = variable_units(obs[obs_name])
    if (
        isinstance(u_fc, str)
        and u_fc.strip()
        and isinstance(u_obs, str)
        and u_obs.strip()
        and not units_equal(u_fc, u_obs)
    ):
        print(
            f"Warning: --forecast {fc_name!r} units={u_fc.strip()!r} and --obs "
            f"{obs_name!r} units={u_obs.strip()!r} differ. Values are compared "
            "as stored.",
            file=sys.stderr,
        )
    if metric != "hits" and threshold != 1.0:
        print(
            f"Note: --threshold {threshold} is ignored for --metric {metric}.",
            file=sys.stderr,
        )

    valid = fc.notnull() & truth.notnull()
    obs_event = None
    if metric == "hits":
        fc_event = fc >= threshold
        obs_event = truth >= threshold
        field = xr.where(fc_event & obs_event, 1, xr.where(fc_event != obs_event, -1, 0))
    elif metric == "bias":
        field = fc - truth
    else:
        field = np.abs(fc - truth)
    field = field.astype("float32").where(valid)
    field.name = _VAR[metric]

    attrs = {
        k: v
        for k, v in forecast[fc_name].attrs.items()
        if k not in ("standard_name", "GRIB_name", "GRIB_paramId")
    }
    attrs["verify_metric"] = metric
    if metric == "hits":
        attrs.update(
            long_name="Event verification",
            units="1",
            flag_values=np.array([-1, 0, 1], dtype=np.int8),
            flag_meanings="disagree below hit",
            event_threshold=threshold,
            event_variable=fc_name if fc_name == obs_name else f"{fc_name},{obs_name}",
        )
    elif metric == "bias":
        attrs["long_name"] = "Forecast bias (forecast − observation)"
    else:
        attrs["long_name"] = "Mean absolute error"
    field.attrs = attrs

    finite = field.notnull()
    if metric == "hits":
        n_obs = int(obs_event.where(finite, False).sum())
        n_hit = int(((field == 1) & finite).sum())
        summary = (
            "hit rate n/a  (0 obs events)"
            if n_obs == 0
            else f"hit rate {100 * n_hit / n_obs:.0f}%  ({n_hit}/{n_obs} obs events)"
        )
    else:
        lat = next((n for n in ("latitude", "lat") if n in field.dims), None)
        u = format_units_for_display(u_fc or u_obs) if (u_fc or u_obs) else ""
        u_suffix = f" {u}" if u else ""
        value = None
        if lat is None:
            summary = f"{metric} n/a  (no latitude dim)"
        else:
            weights = latitude_weights(field[lat]).broadcast_like(field)
            if bool(finite.any()):
                num = (field * weights).where(finite).sum(skipna=True)
                den = weights.where(finite).sum(skipna=True)
                if den != 0 and np.isfinite(float(den)):
                    value = float(num / den)
            if value is None:
                summary = f"{metric} n/a  (no finite cells)"
            elif metric == "bias":
                sign = "+" if value >= 0 else ""
                summary = f"bias {sign}{value:.2g}{u_suffix}  (cos-lat mean)"
            else:
                summary = f"MAE {value:.2g}{u_suffix}  (cos-lat mean)"
    print(f"verify  {summary}")

    out = field.to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    out.attrs["verify_score_summary"] = summary
    return out


if __name__ == "__main__":
    verify()
