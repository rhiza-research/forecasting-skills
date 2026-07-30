# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "cf-units>=3.3",
# ]
# ///
"""Convert data variable(s) to --to-units via cf-units (UDUNITS-2 / CF)."""

from weather_skills_core import Types, UsageError, weather_skill

_SKILL_VERSION = "0.1.8"

# Pint-free canonical keys for optional standard_name restamp after convert.
_STANDARD_NAME_BY_UNITS = {
    "mm/day": "lwe_precipitation_rate",
    "mm day-1": "lwe_precipitation_rate",
    "kg m-2 s-1": "precipitation_flux",
    "kg m**-2 s**-1": "precipitation_flux",
}


def _convert(values, src_units, dst_units):
    """UDUNITS convert; on dim mismatch retry via liquid-water density bridge."""
    import cf_units
    import numpy as np

    src = cf_units.Unit(src_units)
    dst = cf_units.Unit(dst_units)
    arr = np.asarray(values)
    try:
        return src.convert(arr, dst), False
    except ValueError:
        pass
    # 1000 kg m-3: 1 kg m-2 water ≡ 1 mm depth (and same for rates)
    rho = cf_units.Unit("kg m-3") * 1000.0
    for bridged in (src / rho, src * rho):
        try:
            return bridged.convert(arr, dst), True
        except (ValueError, TypeError):
            continue
    raise UsageError(f"{src_units!r} not convertible to {dst_units!r}")


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
    """Convert data variable(s) to --to-units. Omitting --variable converts all."""
    import cf_units

    names = list(dict.fromkeys(variable)) if variable else list(ds.data_vars)
    out = ds.copy()
    for name in names:
        da = ds[name]
        src_units = da.attrs.get("units")
        if not (isinstance(src_units, str) and src_units.strip()):
            raise UsageError(f"variable '{name}' has no units attr")
        try:
            converted, dim_changed = _convert(da.values, src_units, to_units)
        except ValueError as e:
            raise UsageError(f"could not parse units ({src_units!r} -> {to_units!r}): {e}") from None

        source_name = da.attrs.get("standard_name")
        if standard_name is not None:
            new_name = standard_name if standard_name.strip() else None
        else:
            looked = _STANDARD_NAME_BY_UNITS.get(to_units.strip()) or _STANDARD_NAME_BY_UNITS.get(
                str(cf_units.Unit(to_units))
            )
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
