# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Spatially subset a gridded Rhiza Envelope Zarr."""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"

REGIONS = {
    "africa": (23, -20, -37, 59),
    "kenya": (7, 32, -6, 43),
    "ghana": (12, -4, 4, 2),
    "senegal": (17, -17.5, 12, -11),
    "ethiopia": (16, 32, 2, 49),
    "namibia": (-15, 10, -31, 27),
    "botswana": (-15, 18, -28, 31),
    "zambia": (-6, 20, -20, 35),
    "madagascar": (-10, 42, -27, 52),
    "angola": (-5, 12, -18, 24),
}


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
            return json.loads(raw) if raw else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--region", choices=sorted(REGIONS))
    p.add_argument("--bbox", help="N/W/S/E decimal degrees")
    p.add_argument("--dims", help="Override LAT,LON dim names")
    args = p.parse_args()

    if not args.region and not args.bbox:
        print("Error: one of --region or --bbox is required.", file=sys.stderr)
        sys.exit(2)
    if args.bbox:
        try:
            n, w, s, e = (float(x) for x in args.bbox.split("/"))
        except ValueError:
            print("Error: --bbox must be N/W/S/E (decimal degrees).", file=sys.stderr)
            sys.exit(2)
    else:
        n, w, s, e = REGIONS[args.region]

    # Build the cheap fields first; defer _hash_zarr until after the
    # cache-hit check so we don't hash hundreds of MB of zarr on hits.
    partial_entry = {
        "skill": "clip-region",
        "version": _RHIZA_SKILL_VERSION,
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": Path(args.input).name},
    }
    upstream = _load_history(Path(args.input))
    out = Path(args.output)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping clip.",
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
    import numpy as np
    import xarray as xr

    src = Path(args.input)
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if args.dims:
        lat_dim, lon_dim = [x.strip() for x in args.dims.split(",")]
    else:
        try:
            lat_dim = ds.cf["latitude"].name
            lon_dim = ds.cf["longitude"].name
        except KeyError:
            print(
                f"Error: could not identify lat/lon coords via CF metadata or name "
                f"heuristics in {list(ds.coords)}. Pass --dims to override.",
                file=sys.stderr,
            )
            sys.exit(2)

    # length check / monotonicity check / slice
    lat_vals = ds[lat_dim].values
    if len(lat_vals) == 0:
        print("Error: lat axis has length 0; cannot clip.", file=sys.stderr)
        sys.exit(2)
    if len(lat_vals) == 1:
        lat_slice = None
    else:
        diffs = np.diff(lat_vals)
        if (diffs > 0).all():
            lat_slice = slice(s, n)
        elif (diffs < 0).all():
            lat_slice = slice(n, s)
        else:
            print(
                "Error: lat axis is non-monotonic; cannot infer slice orientation. "
                "Re-sort the input or pre-process before clip-region.",
                file=sys.stderr,
            )
            sys.exit(2)
    lon_slice = slice(w, e)

    # Wrap lon to [-180, 180] before the slice so a 0..360 input grid still
    # intersects bboxes that straddle the prime meridian. Mirrors plot.py.
    lon_vals = np.asarray(ds[lon_dim].values)
    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
        ds = ds.assign_coords({lon_dim: ((ds[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)

    if lat_slice is None:
        sub = ds.sel({lon_dim: lon_slice})
    else:
        sub = ds.sel({lat_dim: lat_slice, lon_dim: lon_slice})
    if sub.sizes[lat_dim] == 0 or sub.sizes[lon_dim] == 0:
        print(
            f"Error: clip produced empty result ({dict(sub.sizes)}). "
            f"Check bbox orientation vs. input coord order.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not upstream:
        print(
            "Warning: no upstream rhiza_history on input; treating input as opaque.",
            file=sys.stderr,
        )
    sub.attrs = {
        **ds.attrs,
        "rhiza_history": json.dumps(upstream + [entry], sort_keys=True),
    }
    for v in sub.variables:
        sub[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sub.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({sub.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
