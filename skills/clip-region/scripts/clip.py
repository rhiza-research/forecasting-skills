# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Spatially subset a gridded Rhiza Envelope Zarr."""

import argparse
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

LAT_ALIASES = ("latitude", "lat", "y")
LON_ALIASES = ("longitude", "lon", "x")


def _pick_dim(ds, candidates):
    for name in candidates:
        if name in ds.dims:
            return name
    return None


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

    import xarray as xr

    src = Path(args.input)
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if args.dims:
        lat_dim, lon_dim = [x.strip() for x in args.dims.split(",")]
    else:
        lat_dim = _pick_dim(ds, LAT_ALIASES)
        lon_dim = _pick_dim(ds, LON_ALIASES)
    if lat_dim is None or lon_dim is None:
        print(f"Error: no lat/lon dim found in {list(ds.dims)}.", file=sys.stderr)
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

    sub.attrs = {**ds.attrs, "rhiza_region": str(region_label)}
    for v in sub.variables:
        sub[v].encoding = {}

    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sub.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({sub.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
