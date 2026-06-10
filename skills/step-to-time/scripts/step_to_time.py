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
        epilog=f"skill version: {_RHIZA_SKILL_VERSION}",
    )
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    # Cheap cache-hit pre-check: skill + args + input.basename + upstream
    # history chain. Avoid opening the xarray dataset and hashing the upstream
    # zarr if the output already matches.
    src = Path(args.input)
    partial_entry = {
        "skill": "step-to-time",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name},
    }
    upstream = _load_history(src)
    out = Path(args.output)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping step-to-time.",
            file=sys.stderr,
        )
        return

    import numpy as np
    import xarray as xr

    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

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
    if not np.issubdtype(init_coord.dtype, np.datetime64):
        print(
            f"Error: scalar 'time' coord is not a datetime64 init date (dtype {init_coord.dtype}).",
            file=sys.stderr,
        )
        sys.exit(2)

    init = init_coord.values
    valid_times = init + step.values
    init_iso = str(np.datetime_as_string(init.astype("datetime64[s]")))

    # Drop the scalar init coord, rename the step dim to time, and replace the
    # lead-time labels with the realized valid times. assign_coords creates a
    # fresh coord variable, so the old step attrs do not carry over.
    out_ds = ds.drop_vars("time").rename({"step": "time"})
    out_ds = out_ds.assign_coords(time=("time", valid_times))
    out_ds["time"].attrs.setdefault("standard_name", "time")
    out_ds["time"].attrs.setdefault("axis", "T")

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
