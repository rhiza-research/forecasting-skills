# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "pint>=0.25",
# ]
# ///
"""Convert one data variable to --to-units (pint; water-density bridge)."""

import re
import tokenize

from weather_skills_core import Types, UsageError, weather_skill

_SKILL_VERSION = "0.1.8"
_UDUNITS_POWER_RE = re.compile(r"(?<=[a-zA-Z])(?<![0-9.][eE])(-?\d+)")
_STANDARD_NAME_BY_UNITS = {
    "millimeter / day": "lwe_precipitation_rate",
    "kilogram / meter ** 2 / second": "precipitation_flux",
}


def _normalize_units(units):
    return _UDUNITS_POWER_RE.sub(lambda m: "**" + m.group(1), units)


def _convert(values, src_units, dst_units):
    import pint

    ureg = pint.UnitRegistry()
    rho = ureg.Quantity(1000.0, "kg/m**3")
    src_u = ureg.Unit(_normalize_units(src_units))
    dst_u = ureg.Unit(_normalize_units(dst_units))
    dim_changed = src_u.dimensionality != dst_u.dimensionality
    quantity = ureg.Quantity(values, src_u)
    try:
        magnitude = quantity.to(dst_u).magnitude
    except pint.DimensionalityError:
        magnitude = None
        for divide in (True, False):
            try:
                bridged = quantity / rho if divide else quantity * rho
                magnitude = bridged.to(dst_u).magnitude
                break
            except (pint.DimensionalityError, pint.OffsetUnitCalculusError):
                continue
        if magnitude is None:
            raise pint.DimensionalityError(src_u, dst_u) from None
    return magnitude, dim_changed, str(dst_u)


@weather_skill(
    name="unit-convert",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.ANY],
    optional_args=("variable",),
    hash_input=False,
)
@weather_skill.argument("--to-units", required=True)
@weather_skill.argument("--standard-name", help="Override output CF standard_name.")
def unit_convert(ds, variable, to_units, standard_name):
    """Convert one data variable to --to-units (pint; water-density bridge)."""
    import pint

    if variable is None:
        if len(ds.data_vars) != 1:
            raise UsageError(f"specify --variable; data_vars={list(ds.data_vars)}")
        variable = next(iter(ds.data_vars))
    else:
        variable = variable[0]
    da = ds[variable]
    src_units = da.attrs.get("units")
    if not (isinstance(src_units, str) and src_units.strip()):
        raise UsageError(f"variable '{variable}' has no units attr")
    try:
        converted, dim_changed, canonical = _convert(da.values, src_units, to_units)
    except pint.DimensionalityError:
        raise UsageError(f"{src_units!r} not convertible to {to_units!r}") from None
    except (pint.PintError, ValueError, TypeError, AssertionError, tokenize.TokenError) as e:
        raise UsageError(f"could not parse units ({src_units!r} -> {to_units!r}): {e}") from None

    source_name = da.attrs.get("standard_name")
    if standard_name is not None:
        new_name = standard_name if standard_name.strip() else None
    else:
        looked = _STANDARD_NAME_BY_UNITS.get(canonical)
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
    out = ds.copy()
    out[variable] = da.copy(data=converted)
    out[variable].attrs = attrs
    return out


if __name__ == "__main__":
    unit_convert()
