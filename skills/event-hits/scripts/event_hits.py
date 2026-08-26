# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Classify event hits / misses: forecast vs truth, event = value ≥ threshold."""

import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.units import units_equal, variable_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(
    name="event-hits",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--forecast", type=Dataset("any"), required=True)
@weather_skill.argument("--obs", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--threshold",
    type=float,
    default=1.0,
    help="Event cutoff: a cell is an event when the variable is >= this (default 1.0).",
)
def event_hits(forecast, obs, variable, threshold, **kwargs):
    """Classify event hits / misses: forecast vs truth, event = value ≥ threshold."""
    import numpy as np
    import xarray as xr

    fc_name = variable or auto_variable(forecast)
    obs_name = variable or auto_variable(obs)
    for name, ds, role in ((fc_name, forecast, "forecast"), (obs_name, obs, "obs")):
        if not name or name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --{role}. Available: {list(ds.data_vars)}"
            )

    fc, truth = forecast[fc_name], obs[obs_name]
    if getattr(getattr(fc, "pint", None), "units", None) is not None:
        fc = fc.pint.dequantify()
    if getattr(getattr(truth, "pint", None), "units", None) is not None:
        truth = truth.pint.dequantify()
    if "number" in fc.dims:
        fc = fc.mean("number", keep_attrs=True)
    if "number" in truth.dims:
        truth = truth.mean("number", keep_attrs=True)

    if "step" in fc.dims and "time" not in fc.dims and "time" in truth.dims:
        raise UsageError(
            "forecast still has a step axis; run step-to-time before event-hits "
            "so valid times can align with --obs."
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
            f"{obs_name!r} units={u_obs.strip()!r} differ. The threshold is "
            "applied to each field's numeric values as stored.",
            file=sys.stderr,
        )

    fc, truth = xr.align(fc, truth, join="inner")
    if any(size == 0 for size in fc.sizes.values()):
        raise UsageError(
            "no overlapping coordinates between --forecast and --obs; "
            "align grids (coarsen/clip) and time (step-to-time / aggregate-temporal) first."
        )

    fc_event = fc >= threshold
    obs_event = truth >= threshold
    classified = xr.where(
        fc_event & obs_event,
        1,
        xr.where(fc_event != obs_event, -1, 0),
    ).astype("float32")
    classified = classified.where(fc.notnull() & truth.notnull())
    classified.name = "event_hit"
    classified.attrs = {
        "long_name": "Event verification",
        "units": "1",
        "flag_values": np.array([-1, 0, 1], dtype=np.int8),
        "flag_meanings": "disagree below hit",
        "event_threshold": threshold,
        "event_variable": fc_name if fc_name == obs_name else f"{fc_name},{obs_name}",
    }
    out = classified.to_dataset()
    out.attrs["Conventions"] = "CF-1.13"
    return out


if __name__ == "__main__":
    event_hits()
