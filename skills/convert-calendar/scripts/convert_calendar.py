# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core",
#   "cftime",
#   "numpy",
# ]
#
# [tool.uv.sources]
# weather-skills-core = { path = "../../../../weather-skills-core", editable = true }
# ///
"""Convert time axis to a target CF calendar."""

from weather_skills_core import Types, UsageError, weather_skill
from weather_skills_core.dataset import detect_time_dim

_SKILL_VERSION = "0.1.8"


@weather_skill(
    name="convert-calendar",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.ANY],
    hash_input=False,
)
@weather_skill.argument(
    "--calendar",
    required=True,
    choices=[
        "standard", "gregorian", "proleptic_gregorian", "noleap", "365_day",
        "360_day", "all_leap", "366_day", "julian",
    ],
)
@weather_skill.argument(
    "--align-on",
    choices=["date", "year"],
    help="Required when source or target is 360_day.",
)
@weather_skill.argument("--time-dim", default=None)
def convert_calendar(ds, calendar, align_on, time_dim):
    """Convert time axis to a target CF calendar."""
    import numpy as np

    time_dim = time_dim or detect_time_dim(ds)
    vals = np.asarray(ds[time_dim].values)
    if vals.dtype.kind == "M":
        source = "standard"
    elif vals.size and hasattr(vals.flat[0], "calendar"):
        source = vals.flat[0].calendar
    else:
        source = "standard"
    # xarray requires align_on when 360_day is involved
    if (source == "360_day" or calendar == "360_day") and align_on is None:
        raise UsageError("--align-on required when source or target is 360_day")
    return ds.convert_calendar(calendar, dim=time_dim, align_on=align_on)


if __name__ == "__main__":
    convert_calendar()
