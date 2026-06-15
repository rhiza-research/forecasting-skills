# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "cftime>=1.6",
#   "xarray>=2026.4",
#   "zarr>=3.2",
# ]
# ///
"""Rename one data variable in a weather-skills envelope Zarr to a new name.

Renames a single data variable (``--variable``) to ``--to-name`` via xarray's
``ds.rename``, writing a new envelope with full provenance. Coordinates and
dimensions are out of scope; only a data variable is renamed. All untouched
dims, coords, data variables, and attrs pass through unchanged, and the renamed
variable keeps all of its own attrs.

Composes before ``concat``: obs and forecast sources often name the same
physical quantity differently (IMERG writes ``precip``; IFS writes
``precipitation_surface``), and ``concat`` requires matching variable names
across inputs. Rename each input's variable to a shared name, then concatenate.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"


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
        help="Source data variable to rename. Required if the input has multiple data vars.",
    )
    p.add_argument(
        "--to-name",
        required=True,
        help="New variable name; becomes the output variable's name.",
    )
    args = p.parse_args()

    src = Path(args.input)
    out = Path(args.output)

    # Reject an in-place run: the write path below deletes the output store
    # before the dataset's values (still lazily backed by the source) are
    # read, so renaming onto the input would destroy it. The guard also
    # rejects an output nested inside the input store (or the input nested
    # inside the output): rmtree of either would corrupt the other before the
    # lazily-backed values are read.
    src_r = src.resolve()
    out_r = out.resolve()
    if src_r == out_r or out_r.is_relative_to(src_r) or src_r.is_relative_to(out_r):
        print(
            f"Error: --input ({args.input}) and --output ({args.output}) overlap "
            "as the same store or one nested inside the other; "
            "rename writes to a distinct output path.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Reject an empty/whitespace-only target name before any cache check: an
    # invalid argument must never report a cache hit.
    if not args.to_name.strip():
        print("Error: --to-name must be a non-empty variable name.", file=sys.stderr)
        sys.exit(2)

    # Validate input existence before any hashing or opening: a missing input
    # is a clean "not found" error, not a backend traceback.
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)

    # An existing output must be a directory (a zarr store to replace); the
    # write path below replaces a directory, not a plain file.
    if out.exists() and not out.is_dir():
        print(
            f"Error: --output {args.output} exists and is not a directory.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Cache-hit check: skill + args + input.basename + input.hash + upstream
    # history chain. The recorded input hash forces a recompute on any
    # modification to a same-named input.
    upstream = _load_history(src)
    entry = {
        "skill": "rename",
        "version": _SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name, "hash": _hash_zarr(src)},
    }
    if _cache_hit(out, upstream, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping rename.",
            file=sys.stderr,
        )
        return

    import xarray as xr

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

    # Collision guard: a rename to a name already used by a different
    # variable/coord/dim would clobber it or make the rename ambiguous. The
    # identity rename (--to-name == the source variable) is excepted: it is a
    # valid no-op, so a pipeline renaming to a name some inputs already use
    # still produces the --output store the next step reads.
    if args.to_name != variable:
        existing = set(ds.variables) | set(ds.dims)
        if args.to_name in existing:
            if args.to_name in ds.data_vars:
                kind = "data variable"
            elif args.to_name in ds.coords:
                kind = "coordinate"
            else:
                kind = "dimension"
            print(
                f"Error: --to-name '{args.to_name}' already names an existing "
                f"{kind}; renaming '{variable}' to it would clash.",
                file=sys.stderr,
            )
            sys.exit(2)

    out_ds = ds.rename({variable: args.to_name})

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
        f"Wrote: {args.output} (variable {variable!r} -> {args.to_name!r})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
