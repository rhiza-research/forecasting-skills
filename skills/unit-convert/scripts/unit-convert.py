# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "cf-xarray>=0.11",
#   "cftime>=1.6",
#   "xarray>=2026.4",
#   "zarr>=3.2",
#   "numpy>=2.4",
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

import argparse
import hashlib
import json
import re
import shutil
import sys
import tokenize
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.5"

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


def _hash_zarr(zarr_path: Path) -> str:
    """Stable content hash of a zarr's stored bytes. Walks the zarr dir
    deterministically and hashes relative-path bytes + each file's
    content. Returns sha256 hex digest."""
    h = hashlib.sha256()
    for p in sorted(zarr_path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(zarr_path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _load_history(zarr_path: Path) -> list:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            # compatibility read for the rhiza_ attr prefix; scheduled for removal
            raw = ds.attrs.get("weather_skills_history") or ds.attrs.get("rhiza_history")
    except FileNotFoundError:
        # A not-yet-existing output read during a cache check is a silent miss.
        return []
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list):
        # A present-but-non-array value is malformed under the weather_skills_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed weather_skills_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


def _cache_hit(out: Path, upstream: list, entry: dict) -> bool:
    """Cache check that compares everything except input.hash.

    The hash over the upstream zarr is expensive; the basename + upstream
    history chain is sufficient to identify whether a recompute is needed.
    """
    if not out.exists():
        return False
    history = _load_history(out)
    if len(history) != len(upstream) + 1:
        return False
    if history[:-1] != upstream:
        return False
    last = history[-1]
    last_input = last.get("input") or {}
    entry_input = entry.get("input") or {}
    return (
        last.get("skill") == entry["skill"]
        and last.get("version") == entry["version"]
        and last.get("args") == entry["args"]
        and last_input.get("basename") == entry_input.get("basename")
    )


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


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=f"skill version: {_SKILL_VERSION}",
    )
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--variable",
        "-v",
        help="Variable to convert. Required if the input has multiple data vars.",
    )
    p.add_argument(
        "--to-units",
        required=True,
        help="Target units string (e.g. 'mm/day'); becomes the output variable's units attribute.",
    )
    p.add_argument(
        "--standard-name",
        help=(
            "CF standard_name to write on the output variable. Overrides the "
            "built-in target-units lookup. When omitted, a known target unit sets "
            "the matching name, a dimensionality-changing conversion drops the "
            "now-inconsistent source name, and a same-dimension conversion keeps it."
        ),
    )
    args = p.parse_args()

    src = Path(args.input)
    out = Path(args.output)
    # Reject an in-place run: the write path below deletes the output store
    # before the dataset's unselected variables (still lazily backed by the
    # source) are read, so converting onto the input would destroy them.
    if src.resolve() == out.resolve():
        print(
            f"Error: --input and --output resolve to the same store ({args.output}); "
            "unit-convert writes to a distinct output path.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Cheap cache-hit pre-check: skill + args + input.basename + upstream
    # history chain. Avoid opening the xarray dataset and hashing the upstream
    # zarr if the output already matches.
    partial_entry = {
        "skill": "unit-convert",
        "version": _SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name},
    }
    upstream = _load_history(src)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping unit-convert.",
            file=sys.stderr,
        )
        return

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import pint
    import xarray as xr

    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    data_vars = list(ds.data_vars)
    if args.variable:
        if args.variable not in ds.data_vars:
            print(
                f"Error: variable '{args.variable}' not in data_vars {data_vars}.",
                file=sys.stderr,
            )
            sys.exit(2)
        variable = args.variable
    elif len(data_vars) == 1:
        variable = data_vars[0]
    else:
        print(
            f"Error: input has multiple data vars {data_vars}; specify --variable.",
            file=sys.stderr,
        )
        sys.exit(2)

    da = ds[variable]
    src_units = da.attrs.get("units")
    if not (isinstance(src_units, str) and src_units.strip()):
        print(
            f"Error: variable '{variable}' has no 'units' attr; cannot convert. "
            "A units conversion needs a source units string to convert from.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Materialize the array outside the try so an unrelated error here is not
    # misreported as a units-parse failure.
    values = da.values
    try:
        converted, dim_changed, canonical_target = _convert(values, src_units, args.to_units)
    except pint.DimensionalityError:
        print(
            f"Error: variable '{variable}' units {src_units!r} are not convertible "
            f"to {args.to_units!r} (incompatible dimensions, and no liquid-water "
            "bridge reconciles them).",
            file=sys.stderr,
        )
        sys.exit(2)
    except (pint.PintError, ValueError, TypeError, AssertionError, tokenize.TokenError) as e:
        # pint parses units through the stdlib tokenizer, so a malformed units
        # string can surface as any of these (UndefinedUnitError, a syntax/token
        # error, an AssertionError) rather than a single type. Map them all to a
        # clean exit 2 naming both strings.
        print(
            f"Error: could not parse units for variable '{variable}' "
            f"(source {src_units!r}, target {args.to_units!r}): {e}.",
            file=sys.stderr,
        )
        sys.exit(2)

    new_standard_name = _resolve_standard_name(
        args.standard_name, da.attrs.get("standard_name"), dim_changed, canonical_target
    )
    out_attrs = {**da.attrs, "units": args.to_units}
    if new_standard_name is None:
        out_attrs.pop("standard_name", None)
    else:
        out_attrs["standard_name"] = new_standard_name

    out_da = da.copy(data=converted)
    out_da.attrs = out_attrs

    out_ds = ds.copy()
    out_ds[variable] = out_da

    # Cache miss: now compute the upstream hash and build the final entry.
    entry = {
        **partial_entry,
        "input": {
            "basename": src.name,
            "hash": _hash_zarr(src),
        },
    }
    if not upstream:
        print(
            "Warning: no upstream weather_skills_history on input; treating input as opaque.",
            file=sys.stderr,
        )
    out_ds.attrs = {
        **ds.attrs,
        "weather_skills_history": json.dumps(upstream + [entry], sort_keys=True),
    }
    # compatibility migration for the rhiza_ attr prefix; scheduled for removal
    for _old in ("rhiza_history", "rhiza_source", "rhiza_forecast_init"):
        if _old in out_ds.attrs:
            _new = "weather_skills_" + _old.removeprefix("rhiza_")
            out_ds.attrs.setdefault(_new, out_ds.attrs.pop(_old))
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(
        f"Wrote: {args.output} (variable={variable}, units {src_units!r} -> {args.to_units!r})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
