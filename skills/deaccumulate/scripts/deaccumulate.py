# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "numpy>=2.4",
# ]
# ///
"""Per-step diff of cumulative-since-init forecast variable (clipped ≥0)."""

import re

from weather_skills_core import Types, UsageError, weather_skill

_SKILL_VERSION = "0.1.12"

_TIME = r"(?:second|sec|minute|min|hour|hr|day|s|h|d)"
_RATE_RE = re.compile(
    rf"(?:/\s*{_TIME}\b|\b{_TIME}(?:\*\*|\^)?-1\b|\b(?:W|watts?)\b)",
    re.I,
)


@weather_skill(
    name="deaccumulate",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.ANY],
    optional_args=("variable",),
    hash_input=False,
)
def deaccumulate(ds, variable):
    """Per-step diff of cumulative-since-init forecast variable (clipped ≥0)."""
    import numpy as np

    if variable is None:
        if len(ds.data_vars) != 1:
            raise UsageError(f"specify --variable; data_vars={list(ds.data_vars)}")
        variable = next(iter(ds.data_vars))
    else:
        variable = variable[0]
    da = ds[variable]
    units, name = da.attrs.get("units"), da.attrs.get("standard_name")
    # Refuse rates — differencing them is silently meaningless
    if (isinstance(name, str) and name.strip().lower().endswith(("_rate", "_flux"))) or (
        isinstance(units, str) and _RATE_RE.search(units)
    ):
        raise UsageError(f"'{variable}' looks like a rate; refuse to deaccumulate")
    diffed = da.isel(step=slice(1, None)).copy(
        data=np.clip(
            da.isel(step=slice(1, None)).values - da.isel(step=slice(0, -1)).values,
            a_min=0,
            a_max=None,
        )
    )
    diffed.attrs = dict(da.attrs)
    out = ds.drop_vars(variable).isel(step=slice(1, None))
    out[variable] = diffed
    return out


if __name__ == "__main__":
    deaccumulate()
