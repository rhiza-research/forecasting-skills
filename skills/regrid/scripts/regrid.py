# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "xarray",
#   "xarray-regrid",
#   "zarr",
#   "numpy",
# ]
# ///
"""Linear regridding for Rhiza Envelope Zarr stores.

Generates a target grid at points ``offset + k * resolution`` for integer k,
clipped to the input's lon/lat range, and interpolates onto it linearly via
xarray-regrid. ``(0.25, 0.0)`` aligns with sheerwater's ``global0_25``;
``(0.1, 0.05)`` with ``global0_1``; ``(0.05, 0.025)`` with ``global0_05``.
"""

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path


def _resolve_version() -> str:
    """Return '<git_sha_or_unknown>+<skill_dir_hash>'. The git part comes
    from `git rev-parse HEAD` against the script's parent dir; falls back
    to 'unknown' when not resolvable. The hash part is sha256 of the
    enclosing skill directory's contents."""
    try:
        sha = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parent,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        sha = "unknown"
    h = hashlib.sha256()
    skill_dir = Path(__file__).resolve().parent.parent
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(skill_dir)).encode())
            h.update(p.read_bytes())
    return f"{sha}+{h.hexdigest()}"


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


def _target_axis(coord_vals, resolution: float, offset: float):
    import numpy as np

    vmin = float(np.min(coord_vals))
    vmax = float(np.max(coord_vals))
    # Tolerance on (vmin, vmax) - offset / resolution to keep boundary points
    # that are on-grid up to floating-point noise.
    eps = 1e-9 * max(1.0, abs(vmin), abs(vmax)) / resolution
    k_min = int(math.ceil((vmin - offset) / resolution - eps))
    k_max = int(math.floor((vmax - offset) / resolution + eps))
    if k_max < k_min:
        raise ValueError(
            f"No grid points at offset={offset}, resolution={resolution} "
            f"fall within range [{vmin}, {vmax}]."
        )
    target = offset + np.arange(k_min, k_max + 1) * resolution
    if coord_vals[0] > coord_vals[-1]:
        target = target[::-1]
    return target


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--target-resolution",
        type=float,
        required=True,
        help="Target grid spacing in degrees.",
    )
    p.add_argument(
        "--offset",
        type=float,
        required=True,
        help="Grid offset in degrees; target points fall at offset + k*resolution.",
    )
    p.add_argument("--variable", "-v", help="Restrict to a single data variable.")
    p.add_argument("--dims", help="Override as LAT,LON dim names.")
    args = p.parse_args()

    if args.target_resolution <= 0:
        print("Error: --target-resolution must be > 0.", file=sys.stderr)
        sys.exit(2)

    # Build the cheap fields first; defer _hash_zarr until after the
    # cache-hit check so we don't hash hundreds of MB of zarr on hits.
    partial_entry = {
        "skill": "regrid",
        "version": _resolve_version(),
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output"}},
        "input": {"basename": Path(args.input).name},
    }
    upstream = _load_history(Path(args.input))
    out = Path(args.output)
    if _cache_hit(out, upstream, partial_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping regrid.",
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
    import xarray_regrid  # noqa: F401 — registers the .regrid accessor

    src = Path(args.input)
    if not src.exists():
        print(f"Error: input {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if args.dims:
        lat_dim, lon_dim = [s.strip() for s in args.dims.split(",")]
        if lat_dim not in ds.dims or lon_dim not in ds.dims:
            print(
                f"Error: --dims names not in dataset dims {list(ds.dims)}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        try:
            lat_dim = ds.cf["latitude"].name
            lon_dim = ds.cf["longitude"].name
        except KeyError:
            print(
                f"Error: could not identify lat/lon coords via CF metadata in "
                f"{list(ds.coords)}. Pass --dims to override.",
                file=sys.stderr,
            )
            sys.exit(2)

    if args.variable:
        if args.variable not in ds.data_vars:
            print(
                f"Error: variable '{args.variable}' not in {list(ds.data_vars)}",
                file=sys.stderr,
            )
            sys.exit(2)
        ds = ds[[args.variable]]

    # Wrap lon to [-180, 180] before building the target axis so a 0..360 input
    # grid doesn't produce a target axis spanning ~the whole globe. Mirrors plot.py.
    lon_vals = np.asarray(ds[lon_dim].values)
    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
        ds = ds.assign_coords({lon_dim: ((ds[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)

    new_lat = _target_axis(ds[lat_dim].values, args.target_resolution, args.offset)
    new_lon = _target_axis(ds[lon_dim].values, args.target_resolution, args.offset)
    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )

    print(
        f"Regridding {lat_dim},{lon_dim} (linear) to "
        f"resolution={args.target_resolution} offset={args.offset}: "
        f"{ds.sizes[lat_dim]}x{ds.sizes[lon_dim]} -> {len(new_lat)}x{len(new_lon)}",
        file=sys.stderr,
    )
    out_ds = ds.regrid.linear(target)

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
    print(f"Wrote: {args.output} ({dict(out_ds.sizes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
