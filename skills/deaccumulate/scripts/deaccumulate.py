# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "pint-xarray>=0.6",
# ]
# ///
"""Per-step diff of cumulative-since-init vars (clipped ≥0). Precip → rates."""

from weather_skills_core import Dataset, weather_skill
from weather_skills_core.units import deaccumulate_along_step

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(
    name="deaccumulate",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("forecast"), required=True)
@weather_skill.argument("--variable", "-v")
def deaccumulate(ds, variable, **kwargs):
    """Per-step diff along forecast step. Precip amounts become mm day-1 rates."""
    names = [variable] if variable else None
    return deaccumulate_along_step(ds, names=names)


if __name__ == "__main__":
    deaccumulate()
