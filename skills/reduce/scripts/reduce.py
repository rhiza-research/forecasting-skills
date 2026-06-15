# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cftime>=1.6",
#   "xarray>=2026.4",
#   "zarr>=3.2",
# ]
# ///
"""Collapse one or more named dimensions of a weather-skills envelope Zarr with a statistic.

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
_SKILL_VERSION = "0.1.3"


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
    # Validate input existence before any hashing or cache check: a missing
    # input is a clean user error, not something to discover partway through.
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)

    # Reject an in-place run: the write path below deletes the output store
    # before the dataset's values (still lazily backed by the source) are
    # read, so reducing onto the input would destroy it. The guard also
    # rejects an output nested inside the input store (or the input nested
    # inside the output): rmtree of either would corrupt the other before the
    # lazily-backed values are read.
    src_r = src.resolve()
    out_r = out.resolve()
    if src_r == out_r or out_r.is_relative_to(src_r) or src_r.is_relative_to(out_r):
        print(
            f"Error: --input ({args.input}) and --output ({args.output}) overlap "
            "as the same store or one nested inside the other; "
            "reduce writes to a distinct output path.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Normalize provenance args before stamping so reordered or duplicated
    # flags don't cause spurious cache misses: dedupe + sort --dim, dedupe
    # --variable (order-insensitive selection, but keep them as a sorted list
    # so the recorded args are canonical).
    norm_args = {k: v for k, v in vars(args).items() if k not in {"input", "output"}}
    norm_args["dim"] = sorted(set(args.dim))
    if args.variable is not None:
        norm_args["variable"] = sorted(set(args.variable))

    # The recorded input hash (sha256 over the source's stored bytes) is part
    # of the cache key, so build the full entry up front: a renamed-but-
    # unchanged input misses on basename and a modified same-named input
    # misses on hash.
    upstream = _load_history(src)
    entry = {
        "skill": "reduce",
        "version": _SKILL_VERSION,
        "args": norm_args,
        "input": {
            "basename": src.name,
            "hash": _hash_zarr(src),
        },
    }
    if _cache_hit(out, upstream, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping reduce.",
            file=sys.stderr,
        )
        return

    import xarray as xr

    # Wrap the input open so an existing-but-not-a-Zarr path exits cleanly
    # instead of surfacing a backend traceback.
    try:
        ds = xr.open_zarr(src, consolidated=False)
    except Exception as exc:  # noqa: BLE001 - normalize any backend error
        print(
            f"Error: {src} is not a readable Zarr store ({type(exc).__name__}: {exc}).",
            file=sys.stderr,
        )
        sys.exit(2)

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

        # `std` over a size-1 dim is zero by construction (a single sample has
        # no spread); warn so a degenerate spread field isn't read as real.
        if args.method == "std":
            singleton = [d for d in rdims if da.sizes[d] == 1]
            if singleton:
                print(
                    f"Warning: std of variable '{var}' over size-1 dim(s) "
                    f"{singleton} is zero by construction (a single sample has "
                    "no spread).",
                    file=sys.stderr,
                )

        # `median` over ALL of a dask-backed variable's dims raises
        # NotImplementedError in dask (no flattened-median across chunks), so
        # materialize the variable first. Only the all-dims case is affected;
        # a partial-dims median streams fine.
        if args.method == "median" and da.chunks is not None and set(rdims) == set(da.dims):
            da = da.load()

        if args.method == "sum":
            # min_count=1 keeps an all-missing slice NaN instead of summing to
            # 0 (which would read as a real zero total rather than "no data").
            out_ds[var] = da.sum(dim=rdims, keep_attrs=True, min_count=1)
        elif args.method == "std":
            # ddof=1 is the sample standard deviation (ensemble-spread
            # convention: spread across members is a sample estimate, not the
            # population sigma).
            out_ds[var] = da.std(dim=rdims, keep_attrs=True, ddof=1)
        else:
            fn = {
                "mean": da.mean,
                "min": da.min,
                "max": da.max,
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
