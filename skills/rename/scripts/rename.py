# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
# ]
# ///
"""Rename one data variable in a weather-skills standard dataset Zarr."""

from weather_skills_core import Types, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.3"


def _one_variable(variable):
    if variable is None:
        return None
    if len(variable) != 1:
        raise UsageError(f"--variable must be given once; got {variable!r}")
    return variable[0]


@weather_skill(
    name="rename",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.ANY],
    optional_args=("variable",),
)
@weather_skill.argument(
    "--to-name",
    required=True,
    help="New variable name; becomes the output variable's name.",
)
def rename(ds, variable, to_name):
    """Rename one data variable in a weather-skills standard dataset Zarr."""
    if not to_name.strip():
        raise UsageError("--to-name must be a non-empty variable name.")
    variable = _one_variable(variable)
    data_vars = list(ds.data_vars)
    if variable:
        if variable not in ds.data_vars:
            raise UsageError(f"variable '{variable}' not in data_vars {data_vars}.")
    elif len(data_vars) == 1:
        variable = data_vars[0]
    else:
        raise UsageError(f"input has multiple data vars {data_vars}; specify --variable.")

    if to_name != variable:
        existing = set(ds.variables) | set(ds.dims)
        if to_name in existing:
            if to_name in ds.data_vars:
                kind = "data variable"
            elif to_name in ds.coords:
                kind = "coordinate"
            else:
                kind = "dimension"
            raise UsageError(
                f"--to-name '{to_name}' already names an existing "
                f"{kind}; renaming '{variable}' to it would clash."
            )

    return ds.rename({variable: to_name})


if __name__ == "__main__":
    rename()
