# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cftime>=1.6",
#   "xarray>=2026.4",
#   "zarr>=3.2",
#   "numpy>=2.4",
# ]
# ///
"""Realize a forecast's step axis as wall-clock valid times (time = init + step).

A forecast envelope carries a ``step`` dim (lead time, ``timedelta64``) plus a
scalar ``time`` coord holding the forecast init date. Time-based consumers
(observation comparisons, time-axis plots) need a ``time`` dim instead. This
skill computes ``valid_time = init + step`` and rewrites the envelope with
``step`` replaced by a ``time`` dim labeled with those valid times. All data
variables and other dims (``number``, lat/lon) pass through unchanged; the init
date stays discoverable via the ``rhiza_forecast_init`` dataset attr.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"


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
    """Cache check that compares the full recorded entry, including input.hash.

    This skill takes no args beyond the I/O paths, so the cache key is
    skill/version/args/basename/hash plus the upstream history chain. Comparing
    the recorded ``input.hash`` against the current hash of the input means a
    modified same-named input correctly cache-misses.
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
        and last_input.get("hash") == entry_input.get("hash")
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=f"skill version: {_RHIZA_SKILL_VERSION}",
    )
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    src = Path(args.input)
    out = Path(args.output)

    # Validate input existence before any hashing or opening (N4): a missing
    # input is a clean "not found" error, not a backend traceback.
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)

    import numpy as np
    import xarray as xr

    # Open the input up front, guarding a path that exists but isn't a readable
    # Zarr store (N4): emit a clear message rather than letting a backend
    # traceback escape.
    try:
        ds = xr.open_zarr(src, consolidated=False)
    except Exception as e:
        print(
            f"Error: {src} is not a readable Zarr store ({type(e).__name__}: {e}).",
            file=sys.stderr,
        )
        sys.exit(2)

    # Cache-hit check: skill + args + input.basename + input.hash + upstream
    # history chain. The input is already validated as a readable Zarr above, so
    # hashing it here is safe; comparing the recorded hash means a modified
    # same-named input cache-misses (N1).
    src_hash = _hash_zarr(src)
    upstream = _load_history(src)
    entry = {
        "skill": "step-to-time",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name, "hash": src_hash},
    }
    if _cache_hit(out, upstream, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping step-to-time.",
            file=sys.stderr,
        )
        return

    if "time" in ds.dims and "step" in ds.dims:
        print(
            "Error: input has both a 'time' dimension and a 'step' dimension "
            "(a multi-init/hindcast cube); per-init step realization is not "
            "supported. Select a single init first.",
            file=sys.stderr,
        )
        sys.exit(2)
    if "time" in ds.dims:
        print(
            "Error: input already has a wall-clock time axis "
            "('time' is a dim, not a scalar forecast-init coord); nothing to realize.",
            file=sys.stderr,
        )
        sys.exit(2)
    if "step" not in ds.dims:
        print(
            f"Error: input has no 'step' dim; got dims {list(ds.dims)}.",
            file=sys.stderr,
        )
        sys.exit(2)
    if ds.sizes["step"] == 0:
        print(
            "Error: 'step' dim has length 0; nothing to realize.",
            file=sys.stderr,
        )
        sys.exit(2)
    step = ds["step"]
    if not np.issubdtype(step.dtype, np.timedelta64):
        print(
            f"Error: 'step' coord is not a lead time (dtype {step.dtype}, "
            "expected timedelta64); cannot realize valid times from it.",
            file=sys.stderr,
        )
        sys.exit(2)
    if "time" not in ds.coords or ds["time"].ndim != 0:
        print(
            "Error: input has no scalar 'time' coord holding the forecast init date; "
            f"got coords {list(ds.coords)}.",
            file=sys.stderr,
        )
        sys.exit(2)
    init_coord = ds["time"]
    init = init_coord.values

    # The init may be a standard datetime64 scalar or, for a non-standard model
    # calendar (noleap, 360_day), an object-dtype cftime datetime. Accept both;
    # reject genuinely wrong types (ints, strings).
    import cftime

    is_datetime64 = np.issubdtype(init_coord.dtype, np.datetime64)
    init_scalar = init.item() if hasattr(init, "item") else init
    is_cftime = isinstance(init_scalar, cftime.datetime)
    if not (is_datetime64 or is_cftime):
        print(
            f"Error: scalar 'time' coord is not a datetime64 or cftime init date "
            f"(dtype {init_coord.dtype}, value type {type(init_scalar).__name__}).",
            file=sys.stderr,
        )
        sys.exit(2)

    # Guard a missing/null init before computing (N8). datetime64 NaT compares
    # unequal to itself; a cftime that is somehow null is caught the same way.
    if is_datetime64:
        if np.isnat(init):
            print("Error: init date is missing/NaT.", file=sys.stderr)
            sys.exit(2)
    else:
        if init_scalar is None or init_scalar != init_scalar:
            print("Error: init date is missing/NaT.", file=sys.stderr)
            sys.exit(2)

    if is_datetime64:
        # datetime64 path: compute valid_time = init + step, then cast to a
        # canonical datetime64[ns] axis so the output resolution is consistent
        # regardless of the init/step source resolution (N2/N11). A date-only
        # (datetime64[D]) init inherits midnight for its time-of-day.
        valid_times = (init + step.values).astype("datetime64[ns]")
        init_iso = str(np.datetime_as_string(init.astype("datetime64[s]")))
    else:
        # cftime path: cftime objects support timedelta addition. Build the
        # realized axis as an object array of cftime datetimes (their canonical
        # form). step.values is timedelta64; convert each to a Python timedelta.
        steps_td = step.values.astype("timedelta64[us]")
        valid_times = np.array(
            [init_scalar + td.item() for td in steps_td],
            dtype=object,
        )
        init_iso = init_scalar.isoformat()

    # Reject a non-strictly-increasing valid-time axis (N6): duplicate or
    # out-of-order valid times (e.g. two steps mapping to the same wall-clock).
    # Works for both datetime64 and object/cftime arrays.
    if len(valid_times) > 1 and not all(
        valid_times[i] < valid_times[i + 1] for i in range(len(valid_times) - 1)
    ):
        print(
            "Error: realized valid times are not strictly increasing "
            "(duplicate or out-of-order); cannot build a monotonic time axis.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Drop the scalar init coord, rename the step dim to time, and replace the
    # lead-time labels with the realized valid times. assign_coords creates a
    # fresh coord variable, so the old step attrs do not carry over. A
    # pre-existing 'valid_time' coord would otherwise pass through stale
    # alongside the new realized axis, so drop it too (N10).
    drop = ["time"]
    if "valid_time" in ds.variables:
        drop.append("valid_time")
    out_ds = ds.drop_vars(drop).rename({"step": "time"})
    out_ds = out_ds.assign_coords(time=("time", valid_times))
    out_ds["time"].attrs.setdefault("standard_name", "time")
    out_ds["time"].attrs.setdefault("axis", "T")

    if not upstream:
        print(
            "Warning: no upstream rhiza_history on input; treating input as opaque.",
            file=sys.stderr,
        )
    out_ds.attrs = {
        **ds.attrs,
        "rhiza_forecast_init": init_iso,
        "rhiza_history": json.dumps(upstream + [entry], sort_keys=True),
    }
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(
        f"Wrote: {args.output} (step axis realized as {out_ds.sizes['time']} "
        f"valid times, init {init_iso})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
