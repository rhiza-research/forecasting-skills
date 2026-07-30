# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core",
#   "cftime>=1.6",
# ]
#
# [tool.uv.sources]
# weather-skills-core = { path = "../../../../weather-skills-core", editable = true }
# ///
"""Rename one data variable (omit --variable after select left a single var)."""

from weather_skills_core import Types, weather_skill

_SKILL_VERSION = "0.1.3"


@weather_skill(
    name="rename",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.ANY],
    optional_args=("variable",),
)
@weather_skill.argument("--to-name", required=True, help="New variable name.")
def rename(ds, variable, to_name):
    """Rename one data variable."""
    # No --variable: presume select already left the var of interest.
    name = variable[0] if variable else next(iter(ds.data_vars))
    return ds.rename({name: to_name})


if __name__ == "__main__":
    rename()
