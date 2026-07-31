# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cftime>=1.6",
#   "cf-units>=3.3",
# ]
# ///
"""Convert data variable(s) to --to-units, or --to-standard (temp °C, precip mm)."""

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.units import (
    PRECIP_AMOUNT_STANDARD_NAME,
    PRECIP_RATE_STANDARD_NAME,
    convert_values,
    to_standard_units,
    units_equal,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.8"

_STANDARD_NAME_BY_UNITS = {
    "mm/day": PRECIP_RATE_STANDARD_NAME,
    "mm day-1": PRECIP_RATE_STANDARD_NAME,
    "mm": PRECIP_AMOUNT_STANDARD_NAME,
    "kg m-2 s-1": "precipitation_flux",
    "kg m**-2 s**-1": "precipitation_flux",
}


@weather_skill(
    name="unit-convert",
    version=_SKILL_VERSION,
    inputs=["any"],
    outputs=["any"],
)
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
    import cf_units

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
            converted, dim_changed = da.values, False
        else:
            try:
                converted, dim_changed = convert_values(da.values, src_units, to_units)
            except UsageError as e:
                raise UsageError(
                    f"could not convert units for variable '{name}' "
                    f"({src_units!r} -> {to_units!r}): {e}"
                ) from None

        source_name = da.attrs.get("standard_name")
        if standard_name is not None:
            new_name = standard_name if standard_name.strip() else None
        else:
            looked = _STANDARD_NAME_BY_UNITS.get(to_units.strip())
            if looked is None:
                try:
                    looked = _STANDARD_NAME_BY_UNITS.get(str(cf_units.Unit(to_units)))
                except ValueError:
                    looked = None
            precip = isinstance(source_name, str) and (
                "precipitation" in source_name.lower() or "rainfall" in source_name.lower()
            )
            if looked is not None and (source_name is None or precip):
                new_name = looked
            elif dim_changed:
                new_name = None
            else:
                new_name = source_name
        attrs = {**da.attrs, "units": to_units}
        if new_name is None:
            attrs.pop("standard_name", None)
        else:
            attrs["standard_name"] = new_name
        out[name] = da.copy(data=converted)
        out[name].attrs = attrs
    return out


if __name__ == "__main__":
    unit_convert()
