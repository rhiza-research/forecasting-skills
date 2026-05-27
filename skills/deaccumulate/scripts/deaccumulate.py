# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray>=0.11",
#   "xarray>=2026.4",
#   "zarr>=3.2",
#   "numpy>=2.4",
# ]
# ///
"""Deaccumulate a cumulative-since-init variable along the forecast step axis.

Some forecast variables (e.g. ECMWF S2S ``tp``, surface radiation, evaporation,
SWE) are stored as values accumulated from the forecast initialization time.
This skill converts those to per-step diffs: ``out[i] = arr[i+1] - arr[i]``,
clipped at zero. The output ``step`` coord drops the first input step, so the
resulting axis labels each value with the end of the period it covers.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.1"


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
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--variable",
        "-v",
        help="Variable to deaccumulate. Required if the input has multiple data vars.",
    )
    args = p.parse_args()

    # Cheap cache-hit pre-check: skill + args + input.basename + upstream
    # history chain. Avoid opening the xarray dataset and hashing the upstream
    # zarr if the output already matches.
    src = Path(args.input)
    partial_entry = {
        "skill": "deaccumulate",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": src.name},
    }
    upstream = _load_history(src)
    out = Path(args.output)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping deaccumulate.",
            file=sys.stderr,
        )
        return

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np
    import xarray as xr

    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if "step" not in ds.dims:
        print(
            f"Error: input has no 'step' dim; got dims {list(ds.dims)}.",
            file=sys.stderr,
        )
        sys.exit(2)

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

    da = ds[variable]
    if da.sizes["step"] < 2:
        print(
            f"Error: 'step' dim has length {da.sizes['step']}; need at least 2 to diff.",
            file=sys.stderr,
        )
        sys.exit(2)

    diffed = da.isel(step=slice(1, None)).copy(
        data=np.clip(
            da.isel(step=slice(1, None)).values - da.isel(step=slice(0, -1)).values,
            a_min=0,
            a_max=None,
        )
    )
    diffed.attrs = dict(da.attrs)

    out_ds = ds.drop_vars(variable).isel(step=slice(1, None))
    out_ds[variable] = diffed
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
        "rhiza_history": json.dumps(upstream + [entry], sort_keys=True),
    }
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(
        f"Wrote: {args.output} (variable={variable}, step length {da.sizes['step']} -> {out_ds.sizes['step']})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
