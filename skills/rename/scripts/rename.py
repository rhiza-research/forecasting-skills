# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
# ]
# ///
"""Rename one data variable in a weather-skills envelope Zarr to a new name.

Renames a single data variable (``--variable``) to ``--to-name`` via xarray's
``ds.rename``, writing a new envelope with full provenance. Coordinates and
dimensions are out of scope; only a data variable is renamed. All untouched
dims, coords, data variables, and attrs pass through unchanged, and the renamed
variable keeps all of its own attrs.

Composes before ``concat``: obs and forecast sources often name the same
physical quantity differently (IMERG writes ``precip``; IFS writes
``precipitation_surface``), and ``concat`` requires matching variable names
across inputs. Rename each input's variable to a shared name, then concatenate.
"""

from pathlib import Path

from weather_skills_core import UsageError, WroteSummary, types, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.3"


def _validate_args(args):
    # Reject an empty/whitespace-only target name before any cache check: an
    # invalid argument must never report a cache hit.
    if not args.to_name.strip():
        raise UsageError("--to-name must be a non-empty variable name.")
    # An existing output must be a directory (a zarr store to replace); the
    # write path replaces a directory, not a plain file.
    out = Path(args.output)
    if out.exists() and not out.is_dir():
        raise UsageError(f"--output {args.output} exists and is not a directory.")


@weather_skill(
    "rename",
    _SKILL_VERSION,
    input_type=types.ALL,
    output_type="same",
    variable={
        "mode": types.SINGLE,
        "help": "Source data variable to rename. Required if the input has multiple data vars.",
    },
    extra_args={
        "to_name": {
            "required": True,
            "help": "New variable name; becomes the output variable's name.",
        },
    },
    validate_args=_validate_args,
)
def rename(ds, variable, to_name):
    """Rename one data variable in a weather-skills envelope Zarr to a new name."""
    data_vars = list(ds.data_vars)
    if variable:
        if variable not in ds.data_vars:
            raise UsageError(f"variable '{variable}' not in data_vars {data_vars}.")
    elif len(data_vars) == 1:
        variable = data_vars[0]
    else:
        raise UsageError(f"input has multiple data vars {data_vars}; specify --variable.")

    # Collision guard: a rename to a name already used by a different
    # variable/coord/dim would clobber it or make the rename ambiguous. The
    # identity rename (--to-name == the source variable) is excepted: it is a
    # valid no-op, so a pipeline renaming to a name some inputs already use
    # still produces the --output store the next step reads.
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

    return (
        ds.rename({variable: to_name}),
        WroteSummary(f"variable {variable!r} -> {to_name!r}", replace=True),
    )


if __name__ == "__main__":
    rename()
