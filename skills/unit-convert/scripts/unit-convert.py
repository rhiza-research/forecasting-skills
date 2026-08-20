# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime>=1.6",
#   "pint-xarray>=0.6",
# ]
# ///
"""Convert data variable(s) to --to-units, or --to-standard (temp °C, precip mm)."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    STANDARD,
    convert_dataarray,
    to_standard_units,
    units_equal,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_STANDARD_NAME_BY_UNITS = {
    "mm/day": STANDARD["precip"]["standard_name"],
    "mm day-1": STANDARD["precip"]["standard_name"],
    "mm": STANDARD["precip_amount"]["standard_name"],
    "kg m-2 s-1": "precipitation_flux",
    "kg m**-2 s**-1": "precipitation_flux",
}


@weather_skill(
    name="unit-convert",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--to-units",
    default=None,
    help="Target units (UDUNITS/CF). Mutually exclusive with --to-standard.",
)
@weather_skill.argument(
    "--to-standard",
    action="store_true",
    help="Convert recognized temp/precip vars to degree_Celsius / mm day-1 / mm.",
)
@weather_skill.argument(
    "--standard-name",
    help="CF standard_name override. Ignored with --to-standard.",
)
def unit_convert(ds, variable, to_units, to_standard, standard_name, **kwargs):
    """Convert data variable(s). Omitting --variable converts all (--to-units) or recognized (--to-standard)."""
    if to_standard and to_units:
        raise UsageError("pass only one of --to-units or --to-standard")
    if not to_standard and not to_units:
        raise UsageError("pass --to-units or --to-standard")

    if to_standard:
        names = [variable] if variable else None
        return to_standard_units(ds, variables=names)

    names = [variable] if variable else list(ds.data_vars)
    out = ds.copy()
    for name in names:
        da = ds[name]
        src_units = da.attrs.get("units")
        if not (isinstance(src_units, str) and src_units.strip()):
            raise UsageError(f"variable '{name}' has no units attr")
        if units_equal(src_units, to_units):
            converted_da, density_converted = da, False
        else:
            try:
                converted_da, density_converted = convert_dataarray(da, to_units)
            except UsageError as e:
                raise UsageError(
                    f"could not convert units for variable '{name}' "
                    f"({src_units!r} -> {to_units!r}): {e}"
                ) from None

        source_name = da.attrs.get("standard_name")
        if standard_name is not None:
            new_name = standard_name if standard_name.strip() else None
        else:
            looked = next(
                (sn for key, sn in _STANDARD_NAME_BY_UNITS.items() if units_equal(to_units, key)),
                None,
            )
            precip = isinstance(source_name, str) and (
                "precipitation" in source_name.lower() or "rainfall" in source_name.lower()
            )
            if looked is not None and (source_name is None or precip):
                new_name = looked
            elif density_converted:
                new_name = None
            else:
                new_name = source_name
        attrs = {**converted_da.attrs, "units": to_units}
        if new_name is None:
            attrs.pop("standard_name", None)
        else:
            attrs["standard_name"] = new_name
        out[name] = converted_da
        out[name].attrs = attrs
    return out


if __name__ == "__main__":
    unit_convert()
