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
import json
import shutil
import sys
from pathlib import Path

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


def _upstream_inputs(zarr_path: Path) -> str | None:
    """Read upstream `rhiza_inputs` so this step's cache key chains to upstream changes."""
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            return ds.attrs.get("rhiza_inputs")
    except Exception:
        return None


def _cache_hit(out: Path, inputs: dict) -> bool:
    if not out.exists():
        return False
    try:
        import xarray as xr

        with xr.open_zarr(out, consolidated=False) as ds:
            cached = ds.attrs.get("rhiza_inputs")
    except Exception:
        return False
    return cached == json.dumps(inputs, sort_keys=True)


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
        region_label = args.region or args.bbox
    else:
        n, w, s, e = REGIONS[args.region]
        region_label = args.region

    inputs = {
        "bbox_NWSE": [float(n), float(w), float(s), float(e)],
        "dims": args.dims,
        "input": _upstream_inputs(Path(args.input)),
    }
    out = Path(args.output)
    if _cache_hit(out, inputs):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping clip.",
            file=sys.stderr,
        )
        return

    import cf_xarray  # noqa: F401 — registers the .cf accessor
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

    lat_ascending = ds[lat_dim].values[0] < ds[lat_dim].values[-1]
    lat_slice = slice(s, n) if lat_ascending else slice(n, s)
    lon_slice = slice(w, e)

    sub = ds.sel({lat_dim: lat_slice, lon_dim: lon_slice})
    if sub.sizes[lat_dim] == 0 or sub.sizes[lon_dim] == 0:
        print(
            f"Error: clip produced empty result ({dict(sub.sizes)}). "
            f"Check bbox orientation vs. input coord order.",
            file=sys.stderr,
        )
        sys.exit(1)

    sub.attrs = {
        **ds.attrs,
        "rhiza_region": str(region_label),
        "rhiza_inputs": json.dumps(inputs, sort_keys=True),
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
