# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
# ]
# ///
"""Return analog years for a given --date (stub: 2026 only)."""

import json
import sys

from weather_skills_core import UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# Calendar year → analog years. Stub: only 2026 is populated.
_ANALOGS = {
    2026: (1982, 1997, 2006, 2015, 2019, 2023),
}


def analogs_for(year):
    """Return analog years for ``year``, or raise if that year is not implemented."""
    years = _ANALOGS.get(int(year))
    if years is None:
        implemented = ", ".join(str(y) for y in sorted(_ANALOGS))
        raise UsageError(f"analog years are only implemented for {implemented}; got {year}.")
    return years


@weather_skill(
    name="analog-years",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument("--date", required=True)
@weather_skill.argument(
    "--emit",
    choices=["years", "json"],
    default="years",
    help="stdout format: space-separated years (default) or JSON",
)
def analog_years(date, emit="years", **kwargs):
    """Return analog years for a given --date (stub: 2026 only)."""
    years = analogs_for(date.year)
    print(f"year={date.year}", file=sys.stderr)
    if emit == "json":
        print(
            json.dumps(
                {
                    "date": date.isoformat(),
                    "year": date.year,
                    "years": list(years),
                }
            )
        )
        return
    print(" ".join(str(y) for y in years))


if __name__ == "__main__":
    analog_years()
