# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "ecmwf-api-client",
#   "xarray",
#   "cfgrib",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch ECMWF S2S precipitation (cf + pf) and write a Rhiza Envelope Zarr."""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path


REGIONS = {
    "africa": "23/-20/-37/59",
    "kenya": "7/32/-6/43",
    "ghana": "12/-4/4/2",
    "senegal": "17/-17.5/12/-11",
    "ethiopia": "16/32/2/49",
}


def _require_env() -> None:
    missing = [
        v
        for v in ("ECMWF_API_URL", "ECMWF_API_KEY", "ECMWF_API_EMAIL")
        if not os.environ.get(v)
    ]
    if missing:
        print(
            f"Error: missing required env var(s): {', '.join(missing)}", file=sys.stderr
        )
        sys.exit(2)


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


def _retrieve(server, date: str, area: str, forecast_type: str, target: Path) -> None:
    request = {
        "class": "s2",
        "dataset": "s2s",
        "date": date,
        "expver": "prod",
        "levtype": "sfc",
        "model": "glob",
        "origin": "ecmf",
        "param": "228228",
        "step": "0/168/240/336/480/504/672/720/840/960/1008",
        "stream": "enfo",
        "time": "00:00:00",
        "type": forecast_type,
        "area": area,
        "target": str(target),
    }
    if forecast_type == "pf":
        request["number"] = "1/to/100"
    server.retrieve(request)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", required=True)
    p.add_argument("--region", choices=sorted(REGIONS))
    p.add_argument("--area", help="N/W/S/E bbox overriding --region")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    if not args.area and not args.region:
        print("Error: one of --region or --area is required.", file=sys.stderr)
        sys.exit(2)
    area = args.area or REGIONS[args.region]
    _require_env()

    import xarray as xr
    from ecmwfapi import ECMWFDataServer

    print(f"Fetching ECMWF S2S for area={area} date={args.date}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="ecmwf-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        server = ECMWFDataServer()
        cf_grib = tmp / "cf.grib"
        pf_grib = tmp / "pf.grib"
        print("Retrieving cf...", file=sys.stderr)
        _retrieve(server, args.date, area, "cf", cf_grib)
        print("Retrieving pf...", file=sys.stderr)
        _retrieve(server, args.date, area, "pf", pf_grib)

        print("Decoding GRIB and writing Zarr...", file=sys.stderr)
        cf = xr.open_dataset(cf_grib, engine="cfgrib").assign_coords(number=0)
        pf = xr.open_dataset(pf_grib, engine="cfgrib")
        ds = xr.concat([pf, cf], dim="number").sortby("number")
        ds.attrs.update(
            rhiza_source="ecmwf-s2s",
            rhiza_region=args.region or "",
            rhiza_area_NWSE=area,
            rhiza_date=args.date,
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
