# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray>=0.11",
#   "cftime>=1.6",
#   "xarray>=2026.4",
#   "zarr>=3.2",
#   "numpy>=2.4",
# ]
# ///
"""Deaccumulate a cumulative-since-init variable along the forecast step axis.

Some forecast variables (e.g. ECMWF S2S ``tp``, surface radiation, evaporation,
SWE) are stored as values accumulated from the forecast initialization time.
This skill converts those to per-step diffs: ``out[i] = arr[i+1] - arr[i]``,
clipped at zero. The output ``step`` coord drops the first input step, so the
resulting axis labels each value with the end of the period it covers.
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.6"

# Time-unit tokens that, when they appear as a per-time denominator, mark a
# rate. Deliberately excludes length/mass/etc. tokens (e.g. ``m``) so that a
# per-AREA term such as ``m-2`` / ``m**-2`` in an accumulated depth like
# ``kg m**-2`` is never read as a rate.
_TIME_UNIT_TOKENS = ("second", "sec", "minute", "min", "hour", "hr", "day", "s", "h", "d")

# Slash-denominator rate forms: a ``/`` immediately followed by a time-unit
# token (tolerating surrounding spaces), e.g. ``mm/day``, ``m / s``. Anchored
# on a word boundary at the end of the token so ``/day`` does not also match a
# longer word that merely starts with the token.
_RATE_SLASH_RE = re.compile(
    r"/\s*(?:" + "|".join(_TIME_UNIT_TOKENS) + r")\b",
    re.IGNORECASE,
)

# UDUNITS negative-power rate forms on a time-unit token, restricted to the
# first negative power: ``s-1``, ``s**-1``, ``s^-1``, ``day-1``, etc. The token
# must stand alone (word boundary before it) so that the ``-2`` in ``m-2`` /
# ``m**-2`` (per area) is never matched and the ``m`` length token never
# participates. The power is fixed at exactly ``-1`` (a per-time rate); higher
# negative powers such as ``s-2`` denote acceleration / energy-per-mass (e.g.
# the ``m2 s-2`` of geopotential or CAPE), which are not rates.
_RATE_POWER_RE = re.compile(
    r"\b(?:" + "|".join(_TIME_UNIT_TOKENS) + r")(?:\*\*|\^)?-1\b",
    re.IGNORECASE,
)

# A standalone watt token marks a power (energy per time), which is inherently a
# per-time rate (``W`` = J/s). So a units string such as ``W m-2`` / ``W m**-2``
# / ``W/m2`` (instantaneous radiation flux) is a rate, in contrast to its
# accumulated form ``J m-2``. Match the SI symbol ``W`` on word boundaries (so
# it does not fire inside another token like ``Wb``) or the spelled-out
# ``watt``/``watts``. Under IGNORECASE this also matches a lone lowercase ``w``,
# which is acceptable (there is no standard unit ``w``).
_RATE_WATT_RE = re.compile(
    r"\b(?:W|watts?)\b",
    re.IGNORECASE,
)


def _units_look_like_rate(units: str) -> bool:
    """Return True when a CF ``units`` string carries a per-time denominator or a
    power (watt) term, indicating a rate rather than an accumulated amount.

    Detects three forms:
      - a slash denominator on a time-unit token (``s``, ``sec``, ``second``,
        ``min``, ``minute``, ``h``, ``hr``, ``hour``, ``d``, ``day``):
        ``mm/day``, ``m / s``;
      - a UDUNITS first negative power on a time-unit token: ``s-1``, ``s**-1``,
        ``s^-1``, ``day-1``;
      - a standalone watt token (``W``, ``watt``, ``watts``): ``W m-2``,
        ``W m**-2``, ``W/m2``.

    So ``mm/day``, ``kg m-2 s-1``, ``m s-1``, and ``W m-2`` are rates, while the
    per-area ``m-2`` / ``m**-2`` in ``kg m**-2`` is not (``m`` is not a time
    token), and the higher negative power ``m2 s-2`` (acceleration /
    energy-per-mass) is not a rate.
    """
    if not units:
        return False
    return bool(
        _RATE_SLASH_RE.search(units) or _RATE_POWER_RE.search(units) or _RATE_WATT_RE.search(units)
    )


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
        help="Variable to deaccumulate. Required if the input has multiple data vars.",
    )
    args = p.parse_args()

    # Cheap cache-hit pre-check: skill + args + input.basename + upstream
    # history chain. Avoid opening the xarray dataset and hashing the upstream
    # zarr if the output already matches.
    src = Path(args.input)
    partial_entry = {
        "skill": "deaccumulate",
        "version": _SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name},
    }
    upstream = _load_history(src)
    out = Path(args.output)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping deaccumulate.",
            file=sys.stderr,
        )
        return

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np
    import xarray as xr

    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if "step" not in ds.dims:
        print(
            f"Error: input has no 'step' dim; got dims {list(ds.dims)}.",
            file=sys.stderr,
        )
        sys.exit(2)

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
    if da.sizes["step"] < 2:
        print(
            f"Error: 'step' dim has length {da.sizes['step']}; need at least 2 to diff.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Input-units guard. Deaccumulation differences the variable along step and
    # is only meaningful for a cumulative-since-init accumulated quantity (a
    # depth/amount that grows along step). A per-time rate must not be
    # deaccumulated: differencing a rate yields a meaningless difference-of-rates
    # still labeled with the rate's units. Reject when the metadata positively
    # indicates a rate; warn but proceed when there is no metadata to check.
    units = da.attrs.get("units")
    standard_name = da.attrs.get("standard_name")
    if units is None and standard_name is None:
        print(
            f"Warning: variable '{variable}' has no 'units' or 'standard_name'; "
            "cannot validate that the input is an accumulated quantity. Proceeding.",
            file=sys.stderr,
        )
    else:
        name_is_rate = isinstance(standard_name, str) and standard_name.strip().lower().endswith(
            ("_rate", "_flux")
        )
        units_are_rate = isinstance(units, str) and _units_look_like_rate(units)
        if name_is_rate or units_are_rate:
            print(
                f"Error: variable '{variable}' looks like a per-time rate "
                f"(units={units!r}, standard_name={standard_name!r}); refusing to "
                "deaccumulate. deaccumulate expects a cumulative-since-init "
                "accumulated quantity (a depth/amount such as 'kg m**-2', 'm', or "
                "'mm') that grows along step. A per-time rate (e.g. a CHIRPS/IMERG "
                "daily 'mm/day' product) is already per-period and must not be "
                "deaccumulated.",
                file=sys.stderr,
            )
            sys.exit(2)

    diffed = da.isel(step=slice(1, None)).copy(
        data=np.clip(
            da.isel(step=slice(1, None)).values - da.isel(step=slice(0, -1)).values,
            a_min=0,
            a_max=None,
        )
    )
    diffed.attrs = dict(da.attrs)

    out_ds = ds.drop_vars(variable).isel(step=slice(1, None))
    out_ds[variable] = diffed
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
        f"Wrote: {args.output} (variable={variable}, step length {da.sizes['step']} -> {out_ds.sizes['step']})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
