# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
#   "zarr>=3.2",
# ]
# ///
"""Subtract one weather-skills envelope Zarr from another (A - B).

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
_SKILL_VERSION = "0.1.4"


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


def _to_signed(da, np):
    """Promote a boolean or unsigned-integer DataArray to a type that can
    hold a negative difference, leaving signed-int / float / other dtypes
    untouched. Boolean becomes int16; an unsigned int becomes the next signed
    int wide enough to represent its negatives (uint8->int16, uint16->int32,
    uint32->int64, uint64->int64 — the int64 cast does not clamp, so a uint64
    value above int64 max overflows/wraps), so A - B does not return a
    wrapped-around modulo-2**nbits value."""
    kind = da.dtype.kind
    if kind == "b":
        return da.astype(np.int16)
    if kind == "u":
        widen = {1: np.int16, 2: np.int32, 4: np.int64, 8: np.int64}
        return da.astype(widen.get(da.dtype.itemsize, np.int64))
    return da


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
    """Cache check that compares skill version, flags, each input's name,
    each input's content hash, and upstream history.

    Each recorded input's `hash` (a sha256 over that input's stored bytes) is
    compared too, so any modification to either input forces a recompute even
    when the basename is unchanged, and a renamed-but-unchanged input misses
    on the differing basename. The caller passes a fully-populated `entry`
    (each input carrying its `hash`) so this comparison is exact.
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
        and li.get("hash") == ei["hash"]
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
        epilog=f"skill version: {_SKILL_VERSION}",
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

    # Validate input existence before any hashing or cache check: a missing
    # input is a clean user error, not something to discover partway through.
    missing = [str(ip) for ip in paths if not ip.exists()]
    if missing:
        for m in missing:
            print(f"Error: {m} not found.", file=sys.stderr)
        sys.exit(2)

    # Reject an in-place run: the write path below deletes the output store
    # before the dataset's values (still lazily backed by the inputs) are
    # read, so differencing onto an input would destroy it. The guard also
    # rejects an output nested inside an input store (or an input nested
    # inside the output): rmtree of either would corrupt the other before the
    # lazily-backed values are read.
    out_r = out.resolve()
    for ip in paths:
        ip_r = ip.resolve()
        if ip_r == out_r or out_r.is_relative_to(ip_r) or ip_r.is_relative_to(out_r):
            print(
                f"Error: --output ({args.output}) overlaps with an --input ({ip}) "
                "as the same store or one nested inside the other; "
                "difference writes to a distinct output path.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Normalize provenance args before stamping so reordered or duplicated
    # --variable flags don't cause spurious cache misses: dedupe and sort.
    norm_args = {k: v for k, v in vars(args).items() if k not in {"input", "output"}}
    if args.variable is not None:
        norm_args["variable"] = sorted(set(args.variable))

    # Multi-input entry: `input` is a list of per-input dicts in CLI order
    # (concat's schema), each carrying its content hash and full history
    # chain. The recorded per-input hash (sha256 over the input's stored
    # bytes) is part of the cache key, so build the full entry up front: a
    # renamed-but-unchanged input misses on basename and a modified
    # same-named input misses on hash.
    input_histories = [_load_history(ip) for ip in paths]
    upstream = input_histories[0]
    entry = {
        "skill": "difference",
        "version": _SKILL_VERSION,
        "args": norm_args,
        "input": [
            {"basename": ip.name, "hash": _hash_zarr(ip), "history": hist}
            for ip, hist in zip(paths, input_histories, strict=True)
        ],
    }
    if _cache_hit(out, upstream, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping difference.",
            file=sys.stderr,
        )
        return

    import xarray as xr

    # Wrap each input open so an existing-but-not-a-Zarr path exits cleanly
    # instead of surfacing a backend traceback.
    opened = []
    for ip in paths:
        try:
            opened.append(xr.open_zarr(ip, consolidated=False))
        except Exception as exc:  # noqa: BLE001 - normalize any backend error
            print(
                f"Error: {ip} is not a readable Zarr store ({type(exc).__name__}: {exc}).",
                file=sys.stderr,
            )
            sys.exit(2)
    ds_a, ds_b = opened

    # Flag each input that carries no upstream weather_skills_history as opaque (same
    # note reduce prints for its single input), so a non-reproducible input
    # branch is surfaced per input rather than silently recorded as `[]`.
    for ip, hist in zip(paths, input_histories, strict=True):
        if not hist:
            print(
                f"Warning: no upstream weather_skills_history on input {ip.name}; treating input as opaque.",
                file=sys.stderr,
            )

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
        # Key by full input path (not basename) so two inputs that share a
        # filename in different directories are still compared as distinct
        # inputs rather than collapsing onto one key.
        seen_units = {}
        for ip, ds in zip(paths, (ds_a, ds_b), strict=True):
            u = ds[var].attrs.get("units")
            if isinstance(u, str):
                seen_units[str(ip)] = u.strip()
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

    # Shared dims WITHOUT an index coordinate can't be label-aligned: xarray
    # pairs them positionally by integer position. A size mismatch there can't
    # be reconciled (the subtraction would raise an opaque broadcast error),
    # so exit cleanly naming the dim; on equal sizes warn that the pairing is
    # positional (element i of A minus element i of B), since a coordinateless
    # dim carries no labels to confirm the rows actually correspond.
    shared_dims = set(ds_a.dims) & set(ds_b.dims)
    for d in sorted(shared_dims):
        if d in ds_a.indexes and d in ds_b.indexes:
            continue  # indexed on BOTH sides => label-aligned, not positional
        # A dim indexed on only one side cannot be label-aligned either: xarray
        # has no labels on the other side to join against. Fall through to the
        # positional size check so a mismatch exits cleanly here rather than
        # surfacing an opaque broadcast/alignment error downstream.
        size_a = ds_a.sizes[d]
        size_b = ds_b.sizes[d]
        if size_a != size_b:
            print(
                f"Error: shared dim '{d}' has no index coordinate, so it is "
                f"paired positionally, but the inputs disagree on its size "
                f"({paths[0].name}={size_a}, {paths[1].name}={size_b}); "
                "there is no way to align unlabeled rows of different length.",
                file=sys.stderr,
            )
            sys.exit(2)
        print(
            f"Warning: shared dim '{d}' has no index coordinate; pairing it "
            f"positionally (element i of {paths[0].name} minus element i of "
            f"{paths[1].name}). Verify the rows correspond.",
            file=sys.stderr,
        )

    print(
        f"Differencing {paths[0].name} - {paths[1].name} variables={selected}",
        file=sys.stderr,
    )

    import numpy as np

    # A - B per variable. xarray arithmetic inner-joins the shared dims and
    # broadcasts over dims present on only one side, so a (time, latitude,
    # longitude) field minus a (latitude, longitude) baseline yields
    # per-time anomalies. Arithmetic drops attrs; restore the first input's.
    data_vars = {}
    for var in selected:
        da_a = ds_a[var]
        da_b = ds_b[var]

        # Cast boolean and unsigned-integer operands to a signed/float type
        # before subtracting: bool subtraction is nonsensical (and numpy
        # deprecates it), and unsigned subtraction wraps around modulo
        # 2**nbits, turning a small negative result into a huge positive one.
        # Promote bool -> int16 and unsigned int -> the next signed int wide
        # enough to hold negatives (falling back to int64).
        da_a = _to_signed(da_a, np)
        da_b = _to_signed(da_b, np)

        # An input dim that is already empty before alignment is distinct from
        # an alignment that produced no overlap; report which.
        pre_empty = [
            d
            for d in set(da_a.sizes) | set(da_b.sizes)
            if da_a.sizes.get(d, 1) == 0 or da_b.sizes.get(d, 1) == 0
        ]
        diff = da_a - da_b
        empty = [d for d, s in diff.sizes.items() if s == 0]
        if empty:
            if set(empty) & set(pre_empty):
                print(
                    f"Error: variable '{var}' is already empty along dim(s) "
                    f"{sorted(set(empty) & set(pre_empty))} in an input before "
                    "alignment: there is nothing to subtract.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Error: aligning the inputs left variable '{var}' empty "
                    f"along dim(s) {empty}: the inputs have no overlapping "
                    "coordinate values there, so there is nothing to subtract.",
                    file=sys.stderr,
                )
            sys.exit(2)
        diff.attrs = dict(ds_a[var].attrs)
        data_vars[var] = diff
    out_ds = xr.Dataset(data_vars)

    # `entry` (with per-input hashes) was built above for the cache check and
    # is reused verbatim for the stamp.
    # Top-level chain stays a single linear array — the first input's chain
    # plus this entry — so single-attr readers keep working; the entry's
    # `input` list records every input branch in full.
    out_ds.attrs = {
        **ds_a.attrs,
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
    print(f"Wrote: {args.output} ({out_ds.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
