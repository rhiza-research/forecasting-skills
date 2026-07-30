# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
# ]
# ///
"""Rename one data variable."""

from weather_skills_core import Types, UsageError, weather_skill

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
    if variable is None:
        if len(ds.data_vars) != 1:
            raise UsageError(f"specify --variable; data_vars={list(ds.data_vars)}")
        variable = next(iter(ds.data_vars))
    else:
        variable = variable[0]
    return ds.rename({variable: to_name})


if __name__ == "__main__":
    rename()
