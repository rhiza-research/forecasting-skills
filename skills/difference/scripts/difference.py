# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cftime>=1.6",
#   "xarray>=2026.4",
#   "zarr>=3.2",
# ]
# ///
"""Subtract one Rhiza Envelope Zarr from another (A - B).

Takes exactly two inputs: the first is the minuend (A), the second the
subtrahend (B). Subtraction is xarray-aligned (inner join on shared dims) with
broadcasting over dims present on only one side, so a ``(time, latitude,
longitude)`` field minus a ``(latitude, longitude)`` baseline (e.g. a
time-mean from ``reduce``) yields per-time anomalies. The output keeps the
first input's attrs.
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
    """Cache check that compares everything except each input's hash.

    The hash over an input zarr is expensive; the per-input basename +
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
    last_inputs = last.get("input")
    entry_inputs = entry["input"]
    if not isinstance(last_inputs, list) or len(last_inputs) != len(entry_inputs):
        return False
    inputs_match = all(
        isinstance(li, dict)
        and li.get("basename") == ei["basename"]
        and li.get("history") == ei["history"]
        for li, ei in zip(last_inputs, entry_inputs, strict=True)
    )
    return (
        last.get("skill") == entry["skill"]
        and last.get("version") == entry["version"]
        and last.get("args") == entry["args"]
        and inputs_match
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=f"skill version: {_RHIZA_SKILL_VERSION}",
    )
    p.add_argument(
        "--input",
        "-i",
        action="append",
        required=True,
        help="Input Zarr; pass exactly twice (first = A, the minuend; second = B, the subtrahend)",
    )
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--variable",
        "-v",
        action="append",
        default=None,
        help="Data variable to difference. Repeat once per variable to select "
        "several; each must be a data variable of BOTH inputs. Default (unset) "
        "differences every data variable present in both inputs.",
    )
    args = p.parse_args()

    if len(args.input) != 2:
        print(
            f"Error: --input must be passed exactly twice; got {len(args.input)}.",
            file=sys.stderr,
        )
        sys.exit(2)

    paths = [Path(s) for s in args.input]
    out = Path(args.output)
    # Reject an in-place run: the write path below deletes the output store
    # before the dataset's values (still lazily backed by the inputs) are
    # read, so differencing onto an input would destroy it.
    if any(ip.resolve() == out.resolve() for ip in paths):
        print(
            f"Error: --output resolves to the same store as an --input ({args.output}); "
            "difference writes to a distinct output path.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Build the cheap fields first; defer _hash_zarr until after the
    # cache-hit check so we don't hash hundreds of MB of zarr on hits.
    # Multi-input entry: `input` is a list of per-input dicts in CLI order
    # (concat's schema), each carrying that input's full history chain.
    input_histories = [_load_history(ip) for ip in paths]
    upstream = input_histories[0]
    partial_entry = {
        "skill": "difference",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": [
            {"basename": ip.name, "history": hist}
            for ip, hist in zip(paths, input_histories, strict=True)
        ],
    }
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping difference.",
            file=sys.stderr,
        )
        return

    import xarray as xr

    missing = [str(ip) for ip in paths if not ip.exists()]
    if missing:
        print(f"Error: missing inputs: {missing}", file=sys.stderr)
        sys.exit(2)

    ds_a, ds_b = (xr.open_zarr(ip, consolidated=False) for ip in paths)

    # Variable selection. Explicit --variable names must be data variables of
    # BOTH inputs. Default selection takes every data variable present in
    # both (in the first input's order); the inputs sharing none is an error.
    # Data variables present in only one input are not differenced and are
    # dropped from the output.
    shared = [v for v in ds_a.data_vars if v in ds_b.data_vars]
    if args.variable is not None:
        # De-duplicate while preserving first-seen order so a repeated name
        # doesn't difference a variable twice.
        selected = list(dict.fromkeys(args.variable))
        for var in selected:
            absent = [
                ip.name
                for ip, ds in zip(paths, (ds_a, ds_b), strict=True)
                if var not in ds.data_vars
            ]
            if absent:
                print(
                    f"Error: --variable '{var}' is not a data variable of {absent}. "
                    f"{paths[0].name} has {list(ds_a.data_vars)}; "
                    f"{paths[1].name} has {list(ds_b.data_vars)}.",
                    file=sys.stderr,
                )
                sys.exit(2)
    else:
        if not shared:
            print(
                f"Error: the inputs share no data variables "
                f"({paths[0].name} has {list(ds_a.data_vars)}; "
                f"{paths[1].name} has {list(ds_b.data_vars)}).",
                file=sys.stderr,
            )
            sys.exit(2)
        selected = shared
    dropped = sorted({v for ds in (ds_a, ds_b) for v in ds.data_vars if v not in selected})
    if dropped:
        print(
            f"Note: dropping data variable(s) {dropped} not differenced "
            f"(absent from an input or unselected).",
            file=sys.stderr,
        )

    # Input-units check. The output variable keeps the first input's attrs,
    # including its `units`. If the inputs hold the variable in different
    # units, the subtraction operates on raw values in incompatible scales,
    # so warn and proceed (run unit-convert first to put both inputs on one
    # units basis). Compare only string `units` attrs, stripped of
    # surrounding whitespace so a trailing space is not read as a real
    # difference.
    for var in selected:
        seen_units = {}
        for ip, ds in zip(paths, (ds_a, ds_b), strict=True):
            u = ds[var].attrs.get("units")
            if isinstance(u, str):
                seen_units[ip.name] = u.strip()
        if len(set(seen_units.values())) > 1:
            detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
            print(
                f"Warning: variable '{var}' has differing units across the "
                f"inputs ({detail}). The subtraction operates on the raw "
                f"values, so the result mixes incompatible scales; convert "
                f"the inputs onto one units basis with unit-convert first. "
                f"The output keeps the first input's units.",
                file=sys.stderr,
            )

    print(
        f"Differencing {paths[0].name} - {paths[1].name} variables={selected}",
        file=sys.stderr,
    )

    # A - B per variable. xarray arithmetic inner-joins the shared dims and
    # broadcasts over dims present on only one side, so a (time, latitude,
    # longitude) field minus a (latitude, longitude) baseline yields
    # per-time anomalies. Arithmetic drops attrs; restore the first input's.
    data_vars = {}
    for var in selected:
        diff = ds_a[var] - ds_b[var]
        empty = [d for d, s in diff.sizes.items() if s == 0]
        if empty:
            print(
                f"Error: aligning the inputs left variable '{var}' empty along "
                f"dim(s) {empty}: the inputs have no overlapping coordinate "
                f"values there, so there is nothing to subtract.",
                file=sys.stderr,
            )
            sys.exit(2)
        diff.attrs = dict(ds_a[var].attrs)
        data_vars[var] = diff
    out_ds = xr.Dataset(data_vars)

    # Cache miss: now compute the per-input hashes and build the final entry.
    entry = {
        **partial_entry,
        "input": [
            {"basename": ip.name, "hash": _hash_zarr(ip), "history": hist}
            for ip, hist in zip(paths, input_histories, strict=True)
        ],
    }
    # Top-level chain stays a single linear array — the first input's chain
    # plus this entry — so single-attr readers keep working; the entry's
    # `input` list records every input branch in full.
    out_ds.attrs = {
        **ds_a.attrs,
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
