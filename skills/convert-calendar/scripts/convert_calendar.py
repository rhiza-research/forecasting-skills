# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "cftime",
#   "numpy",
# ]
# ///
"""Convert time axis to a target CF calendar."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_time_dim

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


@weather_skill(
    name="convert-calendar",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--calendar",
    required=True,
    choices=[
        "standard",
        "gregorian",
        "proleptic_gregorian",
        "noleap",
        "365_day",
        "360_day",
        "all_leap",
        "366_day",
        "julian",
    ],
)
@weather_skill.argument(
    "--align-on",
    choices=["date", "year"],
    help="Required when source or target is 360_day.",
)
@weather_skill.argument("--time-dim", default=None)
def convert_calendar(ds, calendar, align_on, time_dim, **kwargs):
    """Convert time axis to a target CF calendar."""
    import numpy as np

    time_dim = detect_time_dim(ds, time_dim)
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
