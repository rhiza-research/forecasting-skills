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
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.units import units_equal, variable_units

from verification import (
    METRICS,
    compute,
    field_attrs,
    format_score,
    lat_dim_for,
    require_obs_on_forecast_grid,
    score_summary,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(
    name="verify",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--forecast", type=Dataset("any"), required=True)
@weather_skill.argument("--obs", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--metric",
    choices=list(METRICS),
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

    fc_da, obs_da = forecast[fc_name], obs[obs_name]

    if "step" in fc_da.dims and "time" not in fc_da.dims and "time" in obs_da.dims:
        raise UsageError(
            "forecast still has a step axis; run step-to-time before verify "
            "so valid times can align with --obs."
        )

    require_obs_on_forecast_grid(forecast, obs)

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

    result = compute(fc_da, obs_da, metric=metric, threshold=threshold)
    attrs = field_attrs(
        metric,
        threshold=threshold,
        fc_name=fc_name,
        obs_name=obs_name,
        source_attrs=dict(forecast[fc_name].attrs),
    )
    result.field.attrs = attrs

    summary = score_summary(
        metric,
        field=result.field,
        obs_event=result.obs_event,
        units=u_fc or u_obs,
    )
    if lat_dim_for(result.field) is not None:
        print(format_score("verify", metric, field=result.field, obs_event=result.obs_event, units=u_fc or u_obs))
    else:
        print(f"verify  {metric}  (no latitude dim for regional score)")

    out = result.field.to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    out.attrs["verify_metric"] = metric
    out.attrs["verify_score_summary"] = summary
    return out


if __name__ == "__main__":
    verify()
