# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "pint-xarray>=0.6",
# ]
# ///
"""Per-step diff of cumulative-since-init vars (clipped ≥0). Precip → rates."""

import re

from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    STANDARD,
    classify_variable,
    convert_values,
    kind_from_units,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_TIME = r"(?:second|sec|minute|min|hour|hr|day|s|h|d)"
_RATE_RE = re.compile(
    rf"(?:/\s*{_TIME}\b|\b{_TIME}(?:\*\*|\^)?-1\b|\b(?:W|watts?)\b)",
    re.I,
)


def _step_delta_days(ds):
    import numpy as np

    steps = np.asarray(ds["step"].values)
    if steps.size < 2:
        raise UsageError("need at least 2 steps to deaccumulate")
    try:
        diffs_ns = np.diff(steps.astype("timedelta64[ns]").astype(np.int64))
    except (TypeError, ValueError) as exc:
        raise UsageError(f"could not diff step axis: {exc}") from None
    if np.any(diffs_ns <= 0):
        raise UsageError("step axis must be strictly increasing")
    return diffs_ns.astype(np.float64) / 1e9 / 86400.0


def _broadcast_along_step(delta_days, dims):
    import numpy as np

    shape = [1] * len(dims)
    shape[dims.index("step")] = -1
    return delta_days.reshape(shape)


@weather_skill(
    name="deaccumulate",
    version=_SKILL_VERSION,
    allow_precip_totals=True,
)
@weather_skill.argument("-i", "--input", type=Dataset('forecast'), required=True, dest='ds')
@weather_skill.argument("--variable", "-v")
def deaccumulate(ds, variable, **kwargs):
    """Per-step diff along forecast step. Precip amounts become mm day-1 rates."""
    import numpy as np

    names = [variable] if variable else list(ds.data_vars)
    out = ds.isel(step=slice(1, None))
    delta_days = _step_delta_days(ds)

    for name in names:
        da = ds[name]
        units = variable_units(da) or da.attrs.get("units")
        std = da.attrs.get("standard_name")
        if (isinstance(std, str) and std.strip().lower().endswith(("_rate", "_flux"))) or (
            isinstance(units, str) and _RATE_RE.search(units)
        ):
            raise UsageError(f"'{name}' looks like a rate; refuse to deaccumulate")
        plain = da.pint.dequantify() if da.pint.units is not None else da
        src_units = plain.attrs.get("units") or units
        sliced = plain.isel(step=slice(1, None))
        diffs = np.clip(
            sliced.values - plain.isel(step=slice(0, -1)).values,
            a_min=0,
            a_max=None,
        )
        kind = classify_variable(name, units=src_units, standard_name=std)
        if kind is None and isinstance(src_units, str) and src_units.strip():
            kind = kind_from_units(src_units)
        attrs = dict(plain.attrs)
        if kind == "precip_amount":
            if "step" not in sliced.dims:
                raise UsageError(f"variable {name!r} has no step dim")
            mm, _ = convert_values(diffs, src_units, STANDARD["precip_amount"]["units"])
            rate = mm / _broadcast_along_step(delta_days, sliced.dims)
            diffed = sliced.copy(data=rate)
            attrs["units"] = STANDARD["precip"]["units"]
            attrs["standard_name"] = STANDARD["precip"]["standard_name"]
        else:
            diffed = sliced.copy(data=diffs)
        diffed.attrs = attrs
        out[name] = diffed
    return out


if __name__ == "__main__":
    deaccumulate()
