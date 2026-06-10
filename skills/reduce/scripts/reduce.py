# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cftime>=1.6",
#   "xarray>=2026.4",
#   "zarr>=3.2",
# ]
# ///
"""Collapse one or more named dimensions of a Rhiza Envelope Zarr with a statistic.

Reduces the selected data variables along the requested dims with one of
``mean``/``std``/``min``/``max``/``sum``/``median`` (NaNs are skipped), e.g.
the ensemble-spread field as the std across ``number``, model disagreement as
the std across a model dim, or a time-mean baseline for anomaly computation.
Data variables that carry none of the requested dims pass through untouched.
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
    p.add_argument(
        "--dim",
        action="append",
        required=True,
        help="Dimension to collapse. Repeat once per dimension to collapse several in one run.",
    )
    p.add_argument(
        "--method",
        required=True,
        choices=["mean", "std", "min", "max", "sum", "median"],
        help="Statistic applied along the collapsed dimension(s).",
    )
    p.add_argument(
        "--variable",
        "-v",
        action="append",
        default=None,
        help="Restrict the reduction to this data variable. Repeat once per "
        "variable to select several; each selected variable must carry every "
        "requested --dim. Default (unset) reduces every data variable that "
        "carries at least one of the requested dims. Unselected or "
        "untouched data variables pass through unchanged.",
    )
    args = p.parse_args()

    src = Path(args.input)
    out = Path(args.output)
    # Reject an in-place run: the write path below deletes the output store
    # before the dataset's values (still lazily backed by the source) are
    # read, so reducing onto the input would destroy it.
    if src.resolve() == out.resolve():
        print(
            f"Error: --input and --output resolve to the same store ({args.output}); "
            "reduce writes to a distinct output path.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Build the cheap fields first; defer _hash_zarr until after the
    # cache-hit check so we don't hash hundreds of MB of zarr on hits.
    partial_entry = {
        "skill": "reduce",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name},
    }
    upstream = _load_history(src)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping reduce.",
            file=sys.stderr,
        )
        return

    # Cache miss: now compute the upstream hash and build the final entry.
    entry = {
        **partial_entry,
        "input": {
            "basename": src.name,
            "hash": _hash_zarr(src),
        },
    }

    import xarray as xr

    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    # De-duplicate the requested dims preserving first-seen order so a
    # repeated name doesn't reduce twice; each must be an actual dim.
    dims = list(dict.fromkeys(args.dim))
    invalid_dims = [d for d in dims if d not in ds.dims]
    if invalid_dims:
        print(
            f"Error: --dim {invalid_dims} not in dims {list(ds.dims)}.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Variable selection. Explicit --variable names must be data variables and
    # must each carry every requested dim. Default selection takes every data
    # variable carrying at least one of the requested dims (each is reduced
    # over the subset of dims it carries); the rest pass through untouched.
    if args.variable is not None:
        data_vars = list(ds.data_vars)
        invalid_vars = [v for v in args.variable if v not in ds.data_vars]
        if invalid_vars:
            print(
                f"Error: --variable {invalid_vars} not data variable(s) of {src}. "
                f"Valid data variables: {data_vars}",
                file=sys.stderr,
            )
            sys.exit(2)
        # De-duplicate while preserving first-seen order so a repeated name
        # doesn't reduce a variable twice.
        selected = list(dict.fromkeys(args.variable))
        for var in selected:
            missing = [d for d in dims if d not in ds[var].dims]
            if missing:
                print(
                    f"Error: variable '{var}' does not carry --dim {missing}; "
                    f"its dims are {list(ds[var].dims)}.",
                    file=sys.stderr,
                )
                sys.exit(2)
    else:
        selected = [v for v in ds.data_vars if any(d in ds[v].dims for d in dims)]
        if not selected:
            detail = ", ".join(f"{v}{tuple(ds[v].dims)}" for v in ds.data_vars)
            print(
                f"Error: no data variable carries any of --dim {dims}. "
                f"Data variables and their dims: {detail}.",
                file=sys.stderr,
            )
            sys.exit(2)

    passthrough = [v for v in ds.data_vars if v not in selected]
    if passthrough:
        print(
            f"Note: passing through unreduced data variable(s) {passthrough}.",
            file=sys.stderr,
        )

    print(
        f"Reducing dims={dims} method={args.method} variables={selected}",
        file=sys.stderr,
    )

    # Reduce each selected variable over the requested dims it carries.
    # keep_attrs=True preserves the variable attrs (units included); this
    # skill performs no unit math or relabeling — `sum` keeps the input
    # units attr unchanged, and unit-convert exists to restamp units when
    # needed. NaNs are skipped (xarray's default skipna).
    out_ds = ds.copy()
    for var in selected:
        da = ds[var]
        rdims = [d for d in dims if d in da.dims]
        fn = {
            "mean": da.mean,
            "std": da.std,
            "min": da.min,
            "max": da.max,
            "sum": da.sum,
            "median": da.median,
        }[args.method]
        out_ds[var] = fn(dim=rdims, keep_attrs=True)

    # A reduced dim disappears from the reduced variables, but its index
    # coordinate (and any auxiliary coordinate on the dim) would otherwise
    # keep the dim alive on the dataset. Drop each requested dim once no
    # data variable carries it; a dim still carried by a pass-through
    # variable stays.
    for d in dims:
        if d in out_ds.dims and all(d not in out_ds[v].dims for v in out_ds.data_vars):
            out_ds = out_ds.drop_dims(d)

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
