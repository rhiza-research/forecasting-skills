# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "sheerwater",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch CHIRPS live precipitation and write a Rhiza Envelope Zarr."""

import argparse
import shutil
import sys
from pathlib import Path


def _stamp_cf_attrs(ds):
    """Stamp CF standard_name/units/axis on spatial + time coords (non-destructive)."""
    for name in ("latitude", "lat", "y"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "latitude")
            ds[name].attrs.setdefault("units", "degrees_north")
            ds[name].attrs.setdefault("axis", "Y")
            break
    for name in ("longitude", "lon", "x"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "longitude")
            ds[name].attrs.setdefault("units", "degrees_east")
            ds[name].attrs.setdefault("axis", "X")
            break
    if "time" in ds.coords:
        ds["time"].attrs.setdefault("standard_name", "time")
        ds["time"].attrs.setdefault("axis", "T")
    return ds


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    from sheerwater.data.chirps import chirps_raw_live

    print(f"Fetching CHIRPS {args.start} -> {args.end}", file=sys.stderr)
    ds = chirps_raw_live(
        args.start, args.end, recompute=False, cache_mode="local_overwrite"
    )
    ds = ds.drop_attrs()
    ds.attrs.update(
        rhiza_source="chirps",
        rhiza_date=args.end,
    )
    _stamp_cf_attrs(ds)
    for v in ds.variables:
        ds[v].encoding = {}

    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
