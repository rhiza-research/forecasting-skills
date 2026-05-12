# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
#   "rioxarray",
# ]
# ///
"""Fetch CHIRPS prelim precipitation from FTP and write a Rhiza Envelope Zarr."""

import argparse
import ftplib
import json
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import rioxarray  # noqa: F401 — registers .rio accessor
import xarray as xr

CHIRPS_FTP_HOST = "ftp.chc.ucsb.edu"
CHIRPS_FTP_DIR = "/pub/org/chc/products/CHIRPS/v3.0/daily/prelim/sat"
CHIRPS_NODATA = -9999.0


def _cache_hit(out: Path, inputs: dict) -> bool:
    """Return True if the zarr at `out` was produced by these same inputs."""
    if not out.exists():
        return False
    try:
        with xr.open_zarr(out, consolidated=True) as ds:
            cached = ds.attrs.get("rhiza_inputs")
    except Exception:
        return False
    return cached == json.dumps(inputs, sort_keys=True)


def _daterange(start: str, end: str):
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    d = s
    while d <= e:
        yield d
        d += timedelta(days=1)


def _download_day_tif(ftp: ftplib.FTP, day: date, dest_dir: Path) -> Path:
    name = f"chirps-v3.0.prelim.{day.year:04d}.{day.month:02d}.{day.day:02d}.tif"
    ftp.cwd(f"{CHIRPS_FTP_DIR}/{day.year:04d}")
    out = dest_dir / name
    with open(out, "wb") as f:
        ftp.retrbinary(f"RETR {name}", f.write)
    return out


def _open_day(tif: Path, day: date) -> xr.DataArray:
    da = rioxarray.open_rasterio(tif, masked=False).squeeze("band", drop=True)
    da = da.where(da != CHIRPS_NODATA)
    da = da.rename({"y": "lat", "x": "lon"})
    if "spatial_ref" in da.coords:
        da = da.drop_vars("spatial_ref")
    da.attrs = {}
    da = da.expand_dims(time=[np.datetime64(day.isoformat(), "ns")])
    return da


def _stamp_cf_attrs(ds: xr.Dataset) -> xr.Dataset:
    if "lat" in ds.coords:
        ds["lat"].attrs.setdefault("standard_name", "latitude")
        ds["lat"].attrs.setdefault("units", "degrees_north")
        ds["lat"].attrs.setdefault("axis", "Y")
    if "lon" in ds.coords:
        ds["lon"].attrs.setdefault("standard_name", "longitude")
        ds["lon"].attrs.setdefault("units", "degrees_east")
        ds["lon"].attrs.setdefault("axis", "X")
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

    inputs = {"start": args.start, "end": args.end}
    out = Path(args.output)
    if _cache_hit(out, inputs):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    print(f"Fetching CHIRPS prelim {args.start} -> {args.end}", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="chirps_") as tmpdir:
        tmp = Path(tmpdir)
        ftp = ftplib.FTP(CHIRPS_FTP_HOST, timeout=60)
        ftp.login()
        try:
            arrs = []
            for day in _daterange(args.start, args.end):
                print(f"  {day.isoformat()}", file=sys.stderr)
                tif = _download_day_tif(ftp, day, tmp)
                arrs.append(_open_day(tif, day))
        finally:
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()

        da = xr.concat(arrs, dim="time")
        da = da.sortby("lat", ascending=True)
        da.name = "precip"
        da.attrs["units"] = "mm/day"
        da.attrs["standard_name"] = "lwe_precipitation_rate"
        da.attrs["long_name"] = "CHIRPS daily precipitation"

        ds = da.to_dataset()
        ds.attrs["rhiza_source"] = "chirps"
        ds.attrs["rhiza_date"] = args.end
        ds.attrs["rhiza_inputs"] = json.dumps(inputs, sort_keys=True)
        _stamp_cf_attrs(ds)
        for v in ds.variables:
            ds[v].encoding = {}

        if out.exists():
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        ds.to_zarr(out, mode="w", consolidated=True)
        print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
