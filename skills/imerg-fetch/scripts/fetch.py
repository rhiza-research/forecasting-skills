# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "sheerwater",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch IMERG live precipitation and write a Rhiza Envelope Zarr."""

import argparse
import shutil
import sys
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--version", default="late", choices=["early", "late", "final"])
    args = p.parse_args()

    from sheerwater.data.imerg import imerg_raw_live

    print(f"Fetching IMERG {args.version} {args.start} -> {args.end}", file=sys.stderr)
    ds = imerg_raw_live(
        args.start,
        args.end,
        version=args.version,
        cache_mode="local_overwrite",
        delayed=False,
        recompute=True,
    )
    if "precipitation" in ds.data_vars:
        ds = ds.rename({"precipitation": "precip"})
    ds = ds.drop_attrs()
    ds.attrs.update(
        rhiza_source="imerg",
        rhiza_date=args.end,
    )
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
