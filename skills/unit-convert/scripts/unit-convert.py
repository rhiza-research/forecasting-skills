# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "pint>=0.25",
# ]
# ///
"""Convert one data variable in a weather-skills envelope Zarr to a target units string.

Reads the variable's ``units`` attr, converts the values to ``--to-units`` with
the ``pint`` units library, and writes a new Zarr whose variable carries the
converted values and the target ``units`` string. Conversion goes through the
actual array as a pint Quantity, so offset units (e.g. ``K`` -> ``degC``) are
handled correctly rather than scaled.

Cross-dimension water conversions are supported via a liquid-water density
bridge (1000 kg m**-3, i.e. 1 kg m**-2 of water == 1 mm of depth): a direct
conversion is attempted first, and on a dimensionality mismatch the source
quantity is retried divided and multiplied by water density. This turns a
precipitation flux such as ``kg m-2 s-1`` into a depth rate such as ``mm/day``.
"""

import re
import tokenize

from weather_skills_core import UsageError, WroteSummary, types, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.8"

# CF/UDUNITS power notation uses a bare signed integer fused to its unit token
# (``m-2``, ``s-1``, ``m2``); pint's parser expects ``m**-2``, ``s**-1``,
# ``m**2``. Match a signed integer immediately preceded by a letter and insert
# the ``**`` operator. A power already written pint-style (``m**-2``) is left
# alone because the digit there follows ``*``/``-``, not a letter. The
# ``(?<![0-9.][eE])`` guard skips a scientific-notation exponent (``1e3``,
# ``1e-6``, ``1.5e3``): there the digit follows an ``e``/``E`` that itself
# follows a digit or dot, and rewriting it to ``1e**3`` would make pint read
# ``e`` as elementary_charge.
_UDUNITS_POWER_RE = re.compile(r"(?<=[a-zA-Z])(?<![0-9.][eE])(-?\d+)")

# CF standard_name keyed on the pint-canonical form of the target units, used to
# keep standard_name consistent with the new units for common geophysical
# conversions. The key is ``str(pint.UnitRegistry().Unit(...))``, so it is robust
# to spelling (``mm/day``, ``mm/d``, ``mm day-1`` all canonicalize the same).
# Both entries name a precipitation quantity; _resolve_standard_name only lets
# the lookup overwrite a source name that is itself a precipitation name (see
# _is_precipitation_name), so a non-precip variable converted to these units is
# not silently relabeled. A future non-precipitation entry would need the gate
# in _resolve_standard_name widened to match.
_STANDARD_NAME_BY_UNITS = {
    "millimeter / day": "lwe_precipitation_rate",
    "kilogram / meter ** 2 / second": "precipitation_flux",
}


def _is_precipitation_name(name) -> bool:
    """True when a CF standard_name denotes a precipitation quantity (e.g.
    ``precipitation_flux``, ``lwe_precipitation_rate``, ``rainfall_rate``,
    ``thickness_of_rainfall_amount``)."""
    return isinstance(name, str) and ("precipitation" in name.lower() or "rainfall" in name.lower())


def _normalize_units(units: str) -> str:
    """Rewrite UDUNITS power notation (``m-2``, ``s-1``, ``m2``) into the
    ``m**-2`` / ``s**-1`` / ``m**2`` form pint parses, leaving a
    scientific-notation exponent (``1e3``, ``1e-6``) untouched."""
    return _UDUNITS_POWER_RE.sub(lambda m: "**" + m.group(1), units)


def _convert(values, src_units: str, dst_units: str):
    """Convert a numpy array from ``src_units`` to ``dst_units`` with pint.

    Returns ``(magnitude, dim_changed, canonical_target)`` where ``dim_changed``
    is True when the source and target dimensionalities differ and
    ``canonical_target`` is the pint-canonical form of the target units (the key
    into ``_STANDARD_NAME_BY_UNITS``).

    Tries a direct conversion first. On a dimensionality mismatch, retries the
    source quantity divided and then multiplied by liquid-water density
    (1000 kg m**-3) so a water amount/flux per unit area (``kg m**-2``,
    ``kg m**-2 s**-1``) reconciles with a depth/depth-rate (``mm``, ``mm/day``).
    At most one density direction can satisfy a given target (density is not
    dimensionless), so the result is unambiguous. Re-raises pint's
    DimensionalityError when no bridge resolves the units; pint raises
    UndefinedUnitError or ValueError when a units string cannot be parsed.
    """
    import pint

    ureg = pint.UnitRegistry()
    rho = ureg.Quantity(1000.0, "kg/m**3")  # liquid-water density
    src_u = ureg.Unit(_normalize_units(src_units))
    dst_u = ureg.Unit(_normalize_units(dst_units))
    dim_changed = src_u.dimensionality != dst_u.dimensionality
    canonical_target = str(dst_u)
    quantity = ureg.Quantity(values, src_u)
    try:
        magnitude = quantity.to(dst_u).magnitude
    except pint.DimensionalityError:
        # Retry through the density bridge. The two attempts are evaluated
        # lazily (the arithmetic is inside the try) because an offset-unit
        # source like ``degC`` raises OffsetUnitCalculusError on ``quantity /
        # rho`` itself, before any ``.to()``. A source that no density direction
        # reconciles (offset units, or genuinely incompatible dims) re-raises as
        # a DimensionalityError so the caller emits the incompatible-units error.
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
    return magnitude, dim_changed, canonical_target


def _resolve_standard_name(override, source_name, dim_changed: bool, canonical_target: str):
    """Pick the output variable's standard_name so it stays consistent with the
    new units. An explicit ``--standard-name`` override wins; else a lookup keyed
    on the canonical target units (applied only when it would not relabel a
    different physical quantity — see below); else, when the conversion changed
    dimensionality, drop the (now-wrong) source name; else preserve the source
    name (a same-dimension conversion keeps a valid name). Returns the name, or
    None to mean "no standard_name on the output". An explicit empty/whitespace
    override means "drop the standard_name" rather than write an empty attr."""
    if override is not None:
        return override if override.strip() else None
    looked_up = _STANDARD_NAME_BY_UNITS.get(canonical_target)
    # Apply the lookup only when it would not overwrite a different physical
    # quantity: the source has no standard_name, or it already names a
    # precipitation quantity (the family the lookup entries belong to). A
    # non-precip flux (e.g. an evaporation flux) converted to these units falls
    # through and is dropped rather than mislabeled as precipitation.
    if looked_up is not None and (source_name is None or _is_precipitation_name(source_name)):
        return looked_up
    if dim_changed:
        return None
    return source_name


@weather_skill(
    "unit-convert",
    _SKILL_VERSION,
    input_type=types.ALL,
    output_type=types.ALL,
    variable={
        "mode": types.SINGLE,
        "help": "Variable to convert. Required if the input has multiple data vars.",
    },
    extra_args={
        "to_units": {
            "required": True,
            "help": "Target units string (e.g. 'mm/day'); becomes the output variable's "
            "units attribute.",
        },
        "standard_name": {
            "help": (
                "CF standard_name to write on the output variable. Overrides the "
                "built-in target-units lookup. When omitted, a known target unit sets "
                "the matching name, a dimensionality-changing conversion drops the "
                "now-inconsistent source name, and a same-dimension conversion keeps it."
            ),
        },
    },
    hash_input=False,
)
def unit_convert(ds, variable, to_units, standard_name):
    """Convert one data variable in a weather-skills envelope Zarr to a target units string."""
    import pint

    data_vars = list(ds.data_vars)
    if variable:
        if variable not in ds.data_vars:
            raise UsageError(f"variable '{variable}' not in data_vars {data_vars}.")
    elif len(data_vars) == 1:
        variable = data_vars[0]
    else:
        raise UsageError(f"input has multiple data vars {data_vars}; specify --variable.")

    da = ds[variable]
    src_units = da.attrs.get("units")
    if not (isinstance(src_units, str) and src_units.strip()):
        raise UsageError(
            f"variable '{variable}' has no 'units' attr; cannot convert. "
            "A units conversion needs a source units string to convert from."
        )

    # Materialize the array outside the try so an unrelated error here is not
    # misreported as a units-parse failure.
    values = da.values
    try:
        converted, dim_changed, canonical_target = _convert(values, src_units, to_units)
    except pint.DimensionalityError:
        raise UsageError(
            f"variable '{variable}' units {src_units!r} are not convertible "
            f"to {to_units!r} (incompatible dimensions, and no liquid-water "
            "bridge reconciles them)."
        ) from None
    except (pint.PintError, ValueError, TypeError, AssertionError, tokenize.TokenError) as e:
        # pint parses units through the stdlib tokenizer, so a malformed units
        # string can surface as any of these (UndefinedUnitError, a syntax/token
        # error, an AssertionError) rather than a single type. Map them all to a
        # clean exit 2 naming both strings.
        raise UsageError(
            f"could not parse units for variable '{variable}' "
            f"(source {src_units!r}, target {to_units!r}): {e}."
        ) from None

    new_standard_name = _resolve_standard_name(
        standard_name, da.attrs.get("standard_name"), dim_changed, canonical_target
    )
    out_attrs = {**da.attrs, "units": to_units}
    if new_standard_name is None:
        out_attrs.pop("standard_name", None)
    else:
        out_attrs["standard_name"] = new_standard_name

    out_da = da.copy(data=converted)
    out_da.attrs = out_attrs

    out_ds = ds.copy()
    out_ds[variable] = out_da
    return out_ds, WroteSummary(
        f"variable={variable}, units {src_units!r} -> {to_units!r}", replace=True
    )


if __name__ == "__main__":
    unit_convert()
