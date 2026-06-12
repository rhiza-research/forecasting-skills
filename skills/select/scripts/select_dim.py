# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cftime>=1.6",
#   "xarray>=2026.4",
#   "zarr>=3.2",
#   "numpy>=2.4",
#   "pandas",
# ]
# ///
"""Select entries along one named dimension of a weather-skills envelope Zarr.

Picks entries along one ``--dim`` either by integer position (``--index``,
repeatable, negative positions count from the end) or by coordinate value
(``--value``, repeatable, parsed against the coord's dtype), writing a new
envelope. A single selection collapses the dim and also drops every coordinate
variable the selection leaves scalar (the dim's own coord and any auxiliary
coord on the collapsed dim), so outputs from different sources align under
``concat`` (e.g. pick the same forecast week from several model envelopes,
then merge them along a new ``model`` dim). Multiple selections keep the dim
with just those entries, in the order given. All untouched dims, coords, data
variables, and attrs pass through unchanged.
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.1"

# Strict integer-position grammar: an optional leading minus and ASCII digits
# only. Floats, whitespace, "+" signs, underscore separators, and non-ASCII
# digits are rejected.
_INDEX_RE = re.compile(r"-?[0-9]+")

# Strict numeric --value grammar: optional leading minus, ASCII-digit integer
# or decimal/scientific float. Underscore separators, nan, and inf are
# rejected (int()/float() would accept them).
_NUM_INT_RE = re.compile(r"-?[0-9]+")
_NUM_FLOAT_RE = re.compile(r"-?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?")


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
    """Cache check that compares skill version, flags, input name, input
    content hash, and upstream history.

    The recorded input `hash` (a sha256 over the upstream zarr's stored bytes)
    is compared too, so any modification to the input forces a recompute even
    when the basename is unchanged, and a renamed-but-unchanged input misses
    on the differing basename. The caller passes a fully-populated `entry`
    (including `input.hash`) so this comparison is exact.
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


def _fmt_coord_value(v) -> str:
    """Render one coord element for an error-message sample."""
    import numpy as np
    import pandas as pd

    if isinstance(v, np.datetime64):
        return str(np.datetime_as_string(v, unit="s"))
    if isinstance(v, np.timedelta64):
        return str(pd.Timedelta(v))
    return str(v)


def _fail_parse(raw: str, dim: str, dtype, expected: str) -> None:
    print(
        f"Error: --value '{raw}' is not parseable for coord '{dim}' (dtype {dtype}); "
        f"expected {expected}.",
        file=sys.stderr,
    )
    sys.exit(2)


def _parse_value(raw: str, coord_vals, dim: str):
    """Parse a ``--value`` literal against the coord's dtype family.

    Returns the parsed value to compare (exactly) against the coord's values,
    or exits 2 with a message naming the dtype and the expected literal form.
    """
    import numpy as np
    import pandas as pd

    dtype = coord_vals.dtype
    if np.issubdtype(dtype, np.datetime64):
        # Gate the literal before np.datetime64 sees it: relative keywords
        # ("now"/"today") and the empty string are not fixed instants, and a
        # timezone suffix (trailing Z or a +HH:MM/-HH:MM-style offset after
        # the time part) would be silently converted to UTC with a numpy
        # UserWarning. Only naive ISO literals reach the parser.
        expected = (
            "a naive ISO datetime string without timezone (e.g. 2026-06-01 or 2026-06-01T06:00)"
        )
        if not raw or raw.lower() in {"now", "today"}:
            _fail_parse(raw, dim, dtype, expected)
        time_part = re.split(r"[T ]", raw, maxsplit=1)
        if raw[-1] in "Zz" or (len(time_part) == 2 and re.search(r"[+-]", time_part[1])):
            _fail_parse(raw, dim, dtype, expected)
        try:
            return np.datetime64(raw)
        except ValueError:
            _fail_parse(raw, dim, dtype, expected)
    if np.issubdtype(dtype, np.timedelta64):
        try:
            return pd.Timedelta(raw).to_timedelta64()
        except ValueError:
            _fail_parse(raw, dim, dtype, "a pandas-style timedelta string (e.g. 7D or 168h)")
    if np.issubdtype(dtype, np.number):
        # Strict decimal grammar: int()/float() accept underscore separators
        # and nan/inf spellings, which are not coordinate literals.
        if _NUM_INT_RE.fullmatch(raw):
            return int(raw)
        if _NUM_FLOAT_RE.fullmatch(raw):
            return float(raw)
        _fail_parse(
            raw,
            dim,
            dtype,
            "an int or float literal (ASCII digits; no underscores, nan, or inf)",
        )
    if dtype.kind == "U":
        return raw
    if dtype.kind == "S":
        return raw.encode()
    if dtype.kind == "O":
        # Object-dtype string coords (e.g. a station_id axis read back from
        # zarr) match verbatim; any other object payload has no literal form.
        if coord_vals.size and isinstance(coord_vals.flat[0], str):
            return raw
        elem = type(coord_vals.flat[0]).__name__ if coord_vals.size else "unknown"
        print(
            f"Error: coord '{dim}' has object dtype with {elem} values, which --value "
            "cannot match against; select by position with --index instead.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"Error: coord '{dim}' has dtype {dtype}, which --value cannot match against; "
        "select by position with --index instead.",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=f"skill version: {_SKILL_VERSION}",
    )
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--dim", required=True, help="Dimension to select along (exactly one).")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument(
        "--index",
        action="append",
        help="Integer position to select (repeat once per position; negative positions "
        "count from the end). Mutually exclusive with --value.",
    )
    sel.add_argument(
        "--value",
        action="append",
        help="Coordinate value to select, parsed against the coord's dtype (repeat once "
        "per value). Mutually exclusive with --index.",
    )
    args = p.parse_args()

    src = Path(args.input)
    out = Path(args.output)

    # Validate input existence before any hashing or opening: a missing input
    # is a clean "not found" error, not a backend traceback.
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)

    # Reject an in-place run: the write path below deletes the output store
    # before the dataset's values (still lazily backed by the source) are
    # read, so selecting onto the input would destroy it. The guard also
    # rejects an output nested inside the input store (or the input nested
    # inside the output): rmtree of either would corrupt the other before the
    # lazily-backed values are read.
    src_r = src.resolve()
    out_r = out.resolve()
    if src_r == out_r or out_r.is_relative_to(src_r) or src_r.is_relative_to(out_r):
        print(
            f"Error: --input ({args.input}) and --output ({args.output}) overlap "
            "as the same store or one nested inside the other; "
            "select writes to a distinct output path.",
            file=sys.stderr,
        )
        sys.exit(2)

    # An existing output must be a directory (a zarr store to replace); the
    # write path below replaces a directory, not a plain file.
    if out.exists() and not out.is_dir():
        print(
            f"Error: --output {args.output} exists and is not a directory.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Structural pass on --index before anything else: every position must
    # satisfy the strict integer grammar, and the canonical int forms (not
    # the raw strings) are what provenance records — "0" and "00" name the
    # same selection, so they must share one cache identity. Given order is
    # preserved.
    positions_given = None
    if args.index is not None:
        for raw in args.index:
            if not _INDEX_RE.fullmatch(raw):
                print(
                    f"Error: --index '{raw}' is not an integer position; expected an "
                    "optionally negative ASCII-digit integer (e.g. 0, 2, -1).",
                    file=sys.stderr,
                )
                sys.exit(2)
        positions_given = [int(raw) for raw in args.index]

    import numpy as np
    import xarray as xr

    # Open the input up front, guarding a path that exists but isn't a readable
    # Zarr store: emit a clear message rather than letting a backend traceback
    # escape.
    try:
        ds = xr.open_zarr(src, consolidated=False)
    except Exception as e:
        print(
            f"Error: {src} is not a readable Zarr store ({type(e).__name__}: {e}).",
            file=sys.stderr,
        )
        sys.exit(2)

    # Cache-hit check: skill + args + input.basename + input.hash + upstream
    # history chain. Repeated --index/--value flags are recorded in the order
    # given — selection order is honored in the output, so the order is part of
    # the cache identity.
    src_hash = _hash_zarr(src)
    upstream = _load_history(src)
    entry_args = {k: v for k, v in vars(args).items() if k not in {"input", "output"}}
    if positions_given is not None:
        # Canonical ints, in the order given.
        entry_args["index"] = positions_given
    entry = {
        "skill": "select",
        "version": _SKILL_VERSION,
        "args": entry_args,
        "input": {"basename": src.name, "hash": src_hash},
    }
    if _cache_hit(out, upstream, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping select.",
            file=sys.stderr,
        )
        return

    if args.dim not in ds.dims:
        print(
            f"Error: --dim '{args.dim}' not in dims {list(ds.dims)}.",
            file=sys.stderr,
        )
        sys.exit(2)
    size = ds.sizes[args.dim]

    if positions_given is not None:
        # Positional checks; the structural grammar pass already ran above.
        given = list(zip(args.index, positions_given, strict=True))
        for raw, pos in given:
            if pos < -size or pos >= size:
                print(
                    f"Error: --index {raw} is out of range for dim '{args.dim}' of "
                    f"size {size} (valid positions {-size}..{size - 1}).",
                    file=sys.stderr,
                )
                sys.exit(2)
        # Two positions addressing the same element — including a negative
        # alias of an already-given position — are an error: pos % size
        # canonicalizes both signs to one position.
        seen: dict = {}
        for raw, pos in given:
            canon = pos % size
            if canon in seen:
                print(
                    f"Error: --index {seen[canon]} and --index {raw} address the same "
                    f"element (position {canon}) of dim '{args.dim}' (size {size}).",
                    file=sys.stderr,
                )
                sys.exit(2)
            seen[canon] = raw
        positions = [pos for _, pos in given]
    else:
        # Structural: --value needs a coordinate variable on the dim to match
        # against; a coord-less dim can only be addressed by position.
        if args.dim not in ds.coords:
            print(
                f"Error: dim '{args.dim}' has no coordinate variable, so --value has "
                "nothing to match against; select by position with --index instead.",
                file=sys.stderr,
            )
            sys.exit(2)
        coord_vals = ds[args.dim].values
        positions = []
        seen = {}
        for raw in args.value:
            parsed = _parse_value(raw, coord_vals, args.dim)
            matched = np.nonzero(coord_vals == parsed)[0]
            if matched.size == 0:
                sample = ", ".join(_fmt_coord_value(v) for v in coord_vals[:8])
                more = "" if size <= 8 else f", ... ({size} values total)"
                print(
                    f"Error: --value '{raw}' not found in coord '{args.dim}'; "
                    f"available values: {sample}{more}.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if matched.size > 1:
                print(
                    f"Error: --value '{raw}' is ambiguous: it matches {matched.size} "
                    f"entries of coord '{args.dim}' (positions {matched.tolist()}); "
                    "use --index to pick one.",
                    file=sys.stderr,
                )
                sys.exit(2)
            pos = int(matched[0])
            if pos in seen:
                print(
                    f"Error: --value '{seen[pos]}' and --value '{raw}' address the same "
                    f"element (position {pos}) of dim '{args.dim}'.",
                    file=sys.stderr,
                )
                sys.exit(2)
            seen[pos] = raw
            positions.append(pos)

    if len(positions) == 1:
        # A single selection collapses the dim. Also drop every coordinate
        # variable the selection left scalar — the dim's own coord and any
        # auxiliary coord on the collapsed dim (e.g. valid_time(step)): a
        # scalar coord differing between two selected outputs would block or
        # pollute their concat along a new dim. Coords that were already
        # scalar on the input pass through untouched.
        pre_scalar = {c for c in ds.coords if ds[c].ndim == 0}
        out_ds = ds.isel({args.dim: positions[0]})
        newly_scalar = [c for c in out_ds.coords if out_ds[c].ndim == 0 and c not in pre_scalar]
        out_ds = out_ds.drop_vars(newly_scalar)
    else:
        # Multiple selections keep the dim with just those entries, in the
        # order given on the command line.
        out_ds = ds.isel({args.dim: positions})

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
    print(f"Wrote: {args.output} ({out_ds.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
