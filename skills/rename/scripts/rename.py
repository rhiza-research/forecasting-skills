# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cftime>=1.6",
# ]
# ///
"""Rename one data variable (omit --variable after select left a single var)."""

from weather_skills_core import weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.3"


@weather_skill(
    name="rename",
    version=_SKILL_VERSION,
    inputs=["any"],
    outputs=["any"],
)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--to-name", required=True, help="New variable name.")
def rename(ds, variable, to_name, **kwargs):
    """Rename one data variable."""
    # No --variable: presume select already left the var of interest.
    name = variable if variable else next(iter(ds.data_vars))
    return ds.rename({name: to_name})


if __name__ == "__main__":
    rename()
