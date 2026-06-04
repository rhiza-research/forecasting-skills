# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
# ]
# ///
"""Temporal aggregation for Rhiza Envelope Zarr stores.

Supports a `time` dim (wall-clock) or a `step` dim (forecast lead time).
For `time`, uses xarray.resample. For `step`, rolls fixed-length windows
expressed as timedelta64 and aggregates each.
"""

import argparse
import datetime as _dt
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.3"

PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10}
RESAMPLE_FREQ = {"daily": "1D", "weekly": "7D", "dekadal": "10D", "monthly": "MS"}
# Bin-width in days used by the backward-anchored time resample path
# (`--anchor-end`). "monthly" uses a 30-day approximation rather than
# calendar months; see SKILL.md for the caveat.
ANCHOR_PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10, "monthly": 30}


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
            raw = ds.attrs.get("rhiza_history")
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
        # A present-but-non-array value is malformed under the rhiza_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed rhiza_history on {zarr_path}; "
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


# Units that, on their own, mark a temperature (an intensive quantity that
# cannot be summed). Compared case-insensitively against the stripped units
# string. Kelvin, Celsius, Fahrenheit, and their common CF/UDUNITS spellings.
_TEMPERATURE_UNITS = {
    "k",
    "degk",
    "degc",
    "celsius",
    "degree_celsius",
    "degrees_celsius",
    "degreec",
    "°c",
    "degf",
    "degree_fahrenheit",
    "degrees_fahrenheit",
    "fahrenheit",
    "°f",
}

# Pressure units. A pressure value is intensive, but a bare pressure unit is
# also used for non-pressure quantities in some conventions, so these only
# count toward an intensive verdict when the variable's standard_name also
# indicates pressure (see `_intensive_reason`).
_PRESSURE_UNITS = {"pa", "hpa", "mbar", "bar"}

# Percentage units. A percentage is intensive and summing it across a window
# is not a physical total. The bare dimensionless unit `1` is excluded: in CF
# it is also the units of dimensionless counts (e.g. number of wet days), which
# are legitimately summable, so flagging it would be a false positive.
_FRACTION_UNITS = {"%", "percent"}


def _intensive_reason(units, standard_name):
    """Return a short reason string when the variable is clearly an intensive
    quantity (one whose values describe a state, not an amount, so summing them
    over a window is not meaningful), or None when there is no high-confidence
    intensive signal.

    Detection is deliberately conservative — it fires only on unambiguous
    temperature, pressure, or fraction/percentage signals — so that extensive
    quantities such as precipitation depth (`mm`, `kg m**-2`) are never flagged
    and ambiguous metadata is left to proceed.
    """
    name = standard_name.strip().lower() if isinstance(standard_name, str) else ""
    units_norm = units.strip().lower() if isinstance(units, str) else ""

    # Temperature by standard_name: `air_temperature`, `sea_surface_temperature`,
    # or any name ending in `_temperature`.
    if name == "air_temperature" or name.endswith("_temperature"):
        return f"standard_name={standard_name!r} denotes a temperature"

    # Temperature by units.
    if units_norm in _TEMPERATURE_UNITS:
        return f"units={units!r} denotes a temperature"

    # Pressure: require both a pressure unit and a pressure-ish standard_name so
    # that a bare pressure unit on an unrelated variable is not misread.
    if units_norm in _PRESSURE_UNITS and "pressure" in name:
        return f"units={units!r} with standard_name={standard_name!r} denotes a pressure"

    # Percentage (`%`/`percent`); the bare dimensionless unit `1` is not flagged
    # because it also covers summable counts.
    if units_norm in _FRACTION_UNITS:
        return f"units={units!r} denotes a dimensionless fraction or percentage"

    return None


# Extensive depth units (an amount that accumulates, so a per-window SUM of a
# rate expressed in "<depth>/day" lands in this depth). Keys are tolerated input
# spellings (matched case-insensitively); values are the canonical spelling the
# relabel emits.
_DEPTH_UNITS = {
    "mm": "mm",
    "kg m**-2": "kg m**-2",
    "kg m-2": "kg m**-2",
    "kg m^-2": "kg m**-2",
}

# Per-day denominator tokens recognized in a "<depth>/day" rate. Restricted to
# the day family on purpose: the sum of N rate samples equals the N-period total
# depth only when each sample spans exactly one denominator unit, which holds
# for daily-cadence inputs but not for sub-daily cadence. Extending to /hr or /s
# would silently mis-total sub-daily inputs.
#
# The token set is split by spelling form so the two are not mixed:
#   - slash form  ("mm/day"):   the divisor is a BARE word.
#   - product form ("mm day-1"): the per-day factor carries a NEGATIVE power.
# Mixing them ("mm/day-1") is a double negation (mm·day, a depth-days, not a
# rate), so the slash path accepts only bare words and the product path accepts
# only negative-power tokens.
_PER_DAY_SLASH_DENOMINATORS = {"day", "days", "d"}
_PER_DAY_POWER_DENOMINATORS = {"day-1", "d-1", "day**-1", "d**-1"}

# CF standard_name remap applied when a recognized precipitation RATE is summed
# into an accumulated depth. Restricted to verified CF rate/amount pairs; this
# is the standard liquid-water-equivalent precipitation rate and its accumulated
# depth. A rate-shaped name NOT in this table is dropped (not mapped to an
# invented amount name) — see `_summed_units_and_name`.
_RATE_TO_AMOUNT_STANDARD_NAME = {
    "lwe_precipitation_rate": "lwe_thickness_of_precipitation_amount",
}

# Suffixes that mark a `standard_name` as a per-time rate (CF rate names end in
# `_rate`; flux names in `_flux`). Same signal `deaccumulate` uses to detect a
# rate. Case-insensitive, compared against the stripped name.
_RATE_NAME_SUFFIXES = ("_rate", "_flux")


def _rate_depth_numerator(units):
    """If `units` is a recognized per-day depth rate (e.g. ``mm/day``,
    ``mm day-1``), return the canonical extensive depth unit a per-window SUM
    should carry (e.g. ``mm``). Otherwise return None.

    Recognized numerators are precipitation depths (``mm``, ``kg m**-2`` and
    tolerated spellings); recognized denominators are the ``day`` family only.
    Matching is case- and whitespace-tolerant. The denominator is taken as the
    slash-delimited tail (``mm/day``, a bare word) or, absent a slash, the
    trailing whitespace-delimited UDUNITS negative-power token (``mm day-1``).
    A bare word and a negative power are not mixed: ``mm/day-1`` is ``mm·day``
    (a double negation), not a rate, so it returns None. Already-extensive units
    (``mm``, ``kg m**-2``) and non-rate units have no such tail and return None.
    """
    if not isinstance(units, str):
        return None
    u = units.strip()
    if not u:
        return None
    if "/" in u:
        head, _, tail = u.rpartition("/")
        valid_denominators = _PER_DAY_SLASH_DENOMINATORS
    else:
        head, _, tail = u.rpartition(" ")
        valid_denominators = _PER_DAY_POWER_DENOMINATORS
    if tail.strip().lower() not in valid_denominators:
        return None
    return _DEPTH_UNITS.get(head.strip().lower())


def _summed_units_and_name(units, standard_name):
    """Return the ``(units, standard_name)`` a SUM output should carry.

    When ``units`` is a recognized per-day depth rate, each summed value is an
    accumulated depth, so the output must stay self-consistent — a rate
    ``standard_name`` on depth units is invalid CF metadata:

    - units drop to the depth numerator;
    - a known rate ``standard_name`` is remapped to its amount form;
    - a rate-shaped name (``_rate``/``_flux`` suffix) with no known amount
      equivalent is dropped — returned as ``None`` so the caller removes the
      attr and warns — rather than left contradicting the new units;
    - a non-rate or absent ``standard_name`` is returned unchanged.

    When ``units`` is not a recognized rate, both values are returned unchanged.
    """
    depth = _rate_depth_numerator(units)
    if depth is None:
        return units, standard_name
    if not isinstance(standard_name, str):
        return depth, standard_name
    stripped = standard_name.strip()
    if stripped in _RATE_TO_AMOUNT_STANDARD_NAME:
        return depth, _RATE_TO_AMOUNT_STANDARD_NAME[stripped]
    if stripped.lower().endswith(_RATE_NAME_SUFFIXES):
        return depth, None
    return depth, standard_name


def _reduce(grouped, method, dim=None):
    fn = {
        "sum": grouped.sum,
        "mean": grouped.mean,
        "max": grouped.max,
        "min": grouped.min,
    }[method]
    if dim is not None:
        return fn(dim=dim, keep_attrs=True)
    return fn(keep_attrs=True)


def _aggregate_time_anchored(ds, dim, period, method, anchor_end):
    """Backward-anchored time resample.

    The LAST bin is the half-open-on-the-left interval
    ``(anchor_end - period_days, anchor_end]``; previous bins are
    ``period_days`` earlier, working backward as long as the bin's start
    is ``>= ds[dim].min()``. Partial bins whose start would fall before
    the first input timestamp are dropped. The output coord for each bin
    is the bin's right edge (``anchor_end`` for the last bin), matching
    the right-edge convention used by ``_aggregate_step``.
    """
    import numpy as np
    import pandas as pd
    import xarray as xr

    period_days = ANCHOR_PERIOD_DAYS[period]
    bin_width = pd.Timedelta(days=period_days)
    times = pd.to_datetime(np.asarray(ds[dim].values))
    if times.size == 0:
        return ds.isel({dim: slice(0, 0)})
    data_min = pd.Timestamp(times.min())

    bins = []
    right = pd.Timestamp(anchor_end)
    while True:
        left = right - bin_width
        if left < data_min:
            break
        bins.append((left, right))
        right = left
    bins.reverse()

    chunks, labels = [], []
    for left, right in bins:
        # (left, right] window: select strictly after `left` and up to and
        # including `right` to match the left-open, right-closed convention.
        sel = ds.sel({dim: slice(left + pd.Timedelta(nanoseconds=1), right)})
        if sel.sizes.get(dim, 0) == 0:
            continue
        chunks.append(_reduce(sel, method=method, dim=dim))
        labels.append(np.datetime64(right))

    if not chunks:
        return ds.isel({dim: slice(0, 0)})
    return xr.concat(chunks, dim=dim).assign_coords({dim: labels})


def _aggregate_step(ds, period, method):
    import numpy as np
    import pandas as pd

    if period == "monthly":
        print(
            "Error: monthly aggregation is not defined for a forecast step dim.",
            file=sys.stderr,
        )
        sys.exit(2)
    days = PERIOD_DAYS[period]
    window = pd.Timedelta(days=days).to_timedelta64()
    steps = ds["step"].values
    if steps.dtype.kind != "m":
        print(f"Error: 'step' dim must be timedelta64, got {steps.dtype}", file=sys.stderr)
        sys.exit(2)
    max_step = steps.max()
    # Emit a bucket (left, right] only if it covers a full `window` of the
    # input step axis, i.e. right <= max_step. Trailing partial buckets are
    # dropped rather than synthesized past max_step. Buckets are left-open
    # and right-closed so that a step value sitting on the period boundary
    # (e.g. step=7d, the END of week 1 for end-of-period-labeled data such
    # as deaccumulated forecasts) lands in the bucket it physically belongs
    # to. The right edge is the bucket label, so downstream consumers
    # (e.g. plot.py) can reconstruct the [right - window, right] panel title
    # correctly.
    edges = np.arange(0, max_step + window, window, dtype=steps.dtype)
    chunks, labels = [], []
    for left in edges[:-1]:
        right = left + window
        if right > max_step:
            continue
        mask = (steps > left) & (steps <= right)
        if not mask.any():
            continue
        sel = ds.isel(step=np.where(mask)[0])
        chunks.append(_reduce(sel, method=method, dim="step"))
        labels.append(left + window)
    import xarray as xr

    return xr.concat(chunks, dim="step").assign_coords(step=labels)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--period", required=True, choices=["daily", "weekly", "dekadal", "monthly"])
    p.add_argument("--method", default="sum", choices=["sum", "mean", "max", "min"])
    p.add_argument(
        "--variable",
        "-v",
        action="append",
        default=None,
        help="Restrict aggregation to this data variable. Repeat once per "
        "variable to select several. The selected data variables are "
        "aggregated and relabeled as usual; other DATA variables are dropped "
        "from the output (coordinates pass through). Default (unset) "
        "aggregates all data variables.",
    )
    p.add_argument("--time-dim")
    p.add_argument(
        "--anchor-end",
        default=None,
        help="ISO date (YYYY-MM-DD). When set, anchors the obs/time "
        "resample so the LAST bin ends at this date and previous bins "
        "are synthesized backward in `period`-day windows. Partial bins "
        "whose start falls before the input's first timestamp are "
        "dropped. Has no effect on the forecast `step` path.",
    )
    args = p.parse_args()

    if args.anchor_end is not None:
        try:
            _dt.date.fromisoformat(args.anchor_end)
        except ValueError as exc:
            print(
                f"Error: --anchor-end '{args.anchor_end}' is not a valid "
                f"ISO date (YYYY-MM-DD): {exc}",
                file=sys.stderr,
            )
            sys.exit(2)

    # Build the cheap fields first; defer _hash_zarr until after the
    # cache-hit check so we don't hash hundreds of MB of zarr on hits.
    partial_entry = {
        "skill": "aggregate-temporal",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": Path(args.input).name},
    }
    upstream = _load_history(Path(args.input))
    out = Path(args.output)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping aggregate.",
            file=sys.stderr,
        )
        return

    # Cache miss: now compute the upstream hash and build the final entry.
    entry = {
        **partial_entry,
        "input": {
            "basename": Path(args.input).name,
            "hash": _hash_zarr(Path(args.input)),
        },
    }

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import xarray as xr

    src = Path(args.input)
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    # Variable selection. When --variable is given, restrict the dataset to the
    # named DATA variable(s) BEFORE aggregating, so that selecting only an
    # extensive variable (e.g. precip) from a mixed precip+temperature input
    # does not trip the intensive-quantity guard on the unselected variable.
    # Indexing with a list of data-var names keeps all coordinates; the
    # unselected data variables are dropped. Each name must be an actual data
    # variable (not a coordinate or a missing name).
    if args.variable is not None:
        data_vars = list(ds.data_vars)
        invalid = [v for v in args.variable if v not in ds.data_vars]
        if invalid:
            print(
                f"Error: --variable {invalid} not data variable(s) of {src}. "
                f"Valid data variables: {data_vars}",
                file=sys.stderr,
            )
            sys.exit(2)
        # De-duplicate while preserving first-seen order so a repeated name
        # doesn't duplicate a column.
        selected = list(dict.fromkeys(args.variable))
        dropped = [v for v in data_vars if v not in selected]
        if dropped:
            print(
                f"Note: dropping unselected data variable(s) {dropped}; "
                f"aggregating only {selected}.",
                file=sys.stderr,
            )
        ds = ds[selected]

    if args.time_dim:
        dim = args.time_dim
        if dim not in ds.dims:
            print(
                f"Error: --time-dim '{dim}' not in dims {list(ds.dims)}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        # CF "T" axis first (finds wall-clock time even when named unusually),
        # then `step` (forecast lead time — timedelta64, not CF T).
        try:
            dim = ds.cf["time"].name
        except KeyError:
            dim = "step" if "step" in ds.dims else None
        if dim is None or dim not in ds.dims:
            print(
                f"Error: no time/step dim identified in {list(ds.dims)}. "
                f"Pass --time-dim to override.",
                file=sys.stderr,
            )
            sys.exit(2)

    print(
        f"Aggregating dim={dim} period={args.period} method={args.method}",
        file=sys.stderr,
    )

    # Method guard. `--method sum` adds the values within each window into a
    # period total. That is meaningful only for an extensive quantity (an
    # amount that accumulates, e.g. precipitation depth), not for an intensive
    # quantity (a state value such as temperature, pressure, or a fraction):
    # the sum of intensive values has no physical interpretation. Reject only
    # high-confidence intensive cases when the method is `sum`; the other
    # reducers (`mean`/`max`/`min`) are always valid and ambiguous metadata is
    # left to proceed.
    if args.method == "sum":
        for var in ds.data_vars:
            reason = _intensive_reason(
                ds[var].attrs.get("units"),
                ds[var].attrs.get("standard_name"),
            )
            if reason is not None:
                print(
                    f"Error: variable '{var}' is an intensive quantity "
                    f"({reason}); '--method sum' adds its values within each "
                    f"window into a period total, but the sum of an intensive "
                    f"quantity is not a physical total and has no meaningful "
                    f"interpretation.",
                    file=sys.stderr,
                )
                sys.exit(2)

    if dim == "step":
        out_ds = _aggregate_step(ds, args.period, args.method)
    elif args.anchor_end is not None:
        import pandas as pd

        anchor_end = pd.Timestamp(args.anchor_end)
        out_ds = _aggregate_time_anchored(ds, dim, args.period, args.method, anchor_end)
    else:
        resampled = ds.resample({dim: RESAMPLE_FREQ[args.period]})
        out_ds = _reduce(resampled, args.method)

    # Units after sum: `_reduce` keeps the input attrs (keep_attrs=True), so a
    # summed precipitation RATE (e.g. mm/day) would otherwise keep its rate
    # units even though each output value is now an accumulated per-window
    # depth. Relabel recognized per-day depth rates to the extensive depth and
    # remap the precipitation-rate standard_name. Only `sum` accumulates into a
    # total; mean/max/min keep the rate units. See SKILL.md "Units after sum".
    if args.method == "sum":
        for var in out_ds.data_vars:
            attrs = out_ds[var].attrs
            old_units = attrs.get("units")
            old_name = attrs.get("standard_name")
            new_units, new_name = _summed_units_and_name(old_units, old_name)
            if new_units == old_units:
                continue
            attrs["units"] = new_units
            if new_name == old_name:
                continue
            if new_name is None:
                # A rate-shaped standard_name with no known amount equivalent:
                # dropping it keeps the output self-consistent (depth units, no
                # rate name) instead of contradicting the relabeled units.
                attrs.pop("standard_name", None)
                print(
                    f"Warning: variable '{var}' summed to an accumulated depth; "
                    f"relabeled units {old_units!r} -> {new_units!r} and dropped "
                    f"the now-inconsistent rate standard_name {old_name!r} "
                    f"(no known amount equivalent). Restamp standard_name if needed.",
                    file=sys.stderr,
                )
            else:
                attrs["standard_name"] = new_name

    if not upstream:
        print(
            "Warning: no upstream rhiza_history on input; treating input as opaque.",
            file=sys.stderr,
        )
    out_ds.attrs = {
        **ds.attrs,
        "rhiza_history": json.dumps(upstream + [entry], sort_keys=True),
    }
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({out_ds.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
