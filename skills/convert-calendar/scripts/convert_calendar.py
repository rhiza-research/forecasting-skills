# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "cftime",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Convert a Rhiza Envelope Zarr's time axis to a target CF calendar.

Wraps xarray's ``Dataset.convert_calendar`` so two datasets on different CF
calendars can be aligned to a common calendar before comparison. Converting to a
standard calendar yields a ``datetime64`` time axis; converting to a model
calendar (``noleap``, ``360_day``, ...) yields object-dtype ``cftime``. Dates not
representable in the target calendar (e.g. Feb 29 when converting to ``noleap``)
are dropped. ``--align-on`` is required whenever the source or target calendar is
``360_day``.
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


def _source_calendar(time_coord) -> str:
    """Best-effort name of the source calendar of a decoded time coordinate.

    A datetime64 axis is the proleptic Gregorian (``standard``) calendar; an
    object-dtype cftime axis carries its calendar on each element. Returns
    ``"standard"`` when the calendar cannot be read off the values.
    """
    import numpy as np

    vals = np.asarray(time_coord.values)
    if vals.dtype.kind == "M":
        return "standard"
    if vals.size and hasattr(vals.flat[0], "calendar"):
        return vals.flat[0].calendar
    return "standard"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--calendar",
        required=True,
        choices=[
            "standard",
            "gregorian",
            "proleptic_gregorian",
            "noleap",
            "365_day",
            "360_day",
            "all_leap",
            "366_day",
            "julian",
        ],
        help="Target CF calendar name (e.g. standard, proleptic_gregorian, "
        "noleap, 360_day, all_leap, julian).",
    )
    p.add_argument(
        "--time-dim",
        help="Name of the time dim when not auto-detectable via CF metadata.",
    )
    p.add_argument(
        "--align-on",
        choices=["date", "year"],
        help="How to map dates across calendars. Required whenever the source "
        "or target calendar is 360_day. 'year' translates dates by relative "
        "position in the year (best for daily/sub-daily); 'date' conserves "
        "month/day and drops invalid dates (best for coarser-than-daily).",
    )
    args = p.parse_args()

    # Build the cheap fields first; defer _hash_zarr until after the
    # cache-hit check so we don't hash hundreds of MB of zarr on hits.
    partial_entry = {
        "skill": "convert-calendar",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": Path(args.input).name},
    }
    upstream = _load_history(Path(args.input))
    out = Path(args.output)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; "
            "skipping convert-calendar.",
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
        print(f"Error: input {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    # Identify the wall-clock time dim. Honor an explicit --time-dim override,
    # else use cf-xarray's CF "T" axis detection (finds time even when named
    # unusually). Calendar conversion only applies to a wall-clock time axis,
    # not a forecast `step` (timedelta64) lead-time axis.
    if args.time_dim:
        time_dim = args.time_dim
        if time_dim not in ds.dims:
            print(
                f"Error: --time-dim '{time_dim}' not in dims {list(ds.dims)}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        try:
            time_dim = ds.cf["time"].name
        except KeyError:
            time_dim = None
        if time_dim is None or time_dim not in ds.dims:
            print(
                f"Error: no time dim identified via CF metadata in {list(ds.dims)}. "
                f"Pass --time-dim to override.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Validate that the resolved dim is actually a wall-clock time axis before
    # touching the calendar machinery. A datetime64 axis (dtype kind "M") or an
    # object-dtype cftime axis (first element carries a `.calendar` attr) is a
    # time axis; a spatial/coordinate dim pointed at by --time-dim is not. An
    # empty axis is also rejected here: it would otherwise let _source_calendar
    # fall back to "standard" and silently bypass the 360_day align-on guard.
    import numpy as np

    time_vals = np.asarray(ds[time_dim].values)
    if time_vals.size == 0:
        print(
            f"Error: time dim '{time_dim}' is empty; nothing to convert.",
            file=sys.stderr,
        )
        sys.exit(2)
    is_datetime64 = time_vals.dtype.kind == "M"
    is_cftime = time_vals.dtype.kind == "O" and hasattr(time_vals.flat[0], "calendar")
    if not (is_datetime64 or is_cftime):
        print(
            f"Error: dim '{time_dim}' is not a time axis "
            f"(dtype {time_vals.dtype}); expected datetime64 or cftime values. "
            "Pass --time-dim pointing at the wall-clock time dim.",
            file=sys.stderr,
        )
        sys.exit(2)

    # xarray requires align_on when 360_day is on either side of the conversion
    # (it cannot otherwise map between a 360-day year and a calendar with months
    # of varying length). Guard up front with a message naming the flag.
    source_calendar = _source_calendar(ds[time_dim])
    if (source_calendar == "360_day" or args.calendar == "360_day") and args.align_on is None:
        print(
            "Error: --align-on is required when the source or target calendar "
            f"is 360_day (source={source_calendar!r}, target={args.calendar!r}). "
            "Pass --align-on date or --align-on year.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(
        f"Converting dim={time_dim} calendar {source_calendar!r} -> "
        f"{args.calendar!r} (align_on={args.align_on!r})",
        file=sys.stderr,
    )
    out_ds = ds.convert_calendar(args.calendar, dim=time_dim, align_on=args.align_on)

    # If every source timestep is unrepresentable in the target calendar (e.g.
    # converting a series that is entirely Feb 29 / Feb 30 dates), xarray drops
    # them all and leaves a zero-length time axis. Refuse to write an empty store.
    n_in = ds.sizes[time_dim]
    if out_ds.sizes.get(time_dim, 0) == 0:
        print(
            f"Error: conversion to calendar {args.calendar!r} dropped all "
            f"{n_in} timesteps (none representable in the target calendar); "
            "nothing to write.",
            file=sys.stderr,
        )
        sys.exit(2)

    if not upstream:
        print(
            "Warning: no upstream rhiza_history on input; treating input as opaque.",
            file=sys.stderr,
        )
    out_ds.attrs = {
        **ds.attrs,
        "rhiza_history": json.dumps(upstream + [entry], sort_keys=True),
    }
    # Clear stale per-variable encoding before write. The input's time coord
    # carries `units`/`calendar` encoding for the OLD calendar; re-encoding the
    # new calendar axis with that stale encoding would corrupt the time values.
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({dict(out_ds.sizes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
