# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "ecmwf-datastores-client==0.4.2",
#   "requests",
#   "xarray",
#   "cfgrib",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch ECMWF S2S precipitation (cf + pf) and write a Rhiza Envelope Zarr."""

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REGIONS = {
    "africa": [23.0, -20.0, -37.0, 59.0],
    "kenya": [7.0, 32.0, -6.0, 43.0],
    "ghana": [12.0, -4.0, 4.0, 2.0],
    "senegal": [17.0, -17.5, 12.0, -11.0],
    "ethiopia": [16.0, 32.0, 2.0, 49.0],
    "namibia": [-15.0, 10.0, -31.0, 27.0],
    "botswana": [-15.0, 18.0, -28.0, 31.0],
    "zambia": [-6.0, 20.0, -20.0, 35.0],
    "madagascar": [-10.0, 42.0, -27.0, 52.0],
    "angola": [-5.0, 12.0, -18.0, 24.0],
}

LEADTIME_HOURS = ["0", "168", "240", "336", "480", "504", "672", "720", "840", "960", "1008"]

S2S_LICENCE_URL = "https://ecds.ecmwf.int/datasets/s2s-forecasts?tab=download#manage-licences"


def _require_env() -> None:
    missing = [v for v in ("ECMWF_DATASTORES_URL", "ECMWF_DATASTORES_KEY") if not os.environ.get(v)]
    if missing:
        print(f"Error: missing required env var(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)


def _cache_hit(out: Path, inputs: dict) -> bool:
    """Return True if the zarr at `out` was produced by these same inputs."""
    if not out.exists():
        return False
    try:
        import xarray as xr

        with xr.open_zarr(out, consolidated=True) as ds:
            cached = ds.attrs.get("rhiza_inputs")
    except Exception:
        return False
    return cached == json.dumps(inputs, sort_keys=True)


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


def _submit(client, request: dict):
    """Submit an s2s-forecasts retrieval; surface a clean message on the licence-not-accepted case."""
    import requests

    try:
        return client.submit("s2s-forecasts", request)
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        if (
            resp is not None
            and resp.status_code == 403
            and "licences not accepted" in str(e).lower()
        ):
            print(
                "ERROR: ECDS retrieval blocked: required licences not accepted on s2s-forecasts.\n"
                f"Action: open {S2S_LICENCE_URL} in a browser, log in to ECDS, "
                "accept the required licences, then re-run this skill.",
                file=sys.stderr,
            )
            sys.exit(1)
        raise


def _build_request(date_iso: str, area: list[float], forecast_type: str) -> dict:
    d = dt.date.fromisoformat(date_iso)
    return {
        "origin": "ecmwf",
        "level_type": "single_level",
        "variable": ["total_precipitation"],
        "year": [str(d.year)],
        "month": [f"{d.month:02d}"],
        "day": [f"{d.day:02d}"],
        "time": ["00:00"],
        "leadtime_hour": LEADTIME_HOURS,
        "forecast_type": forecast_type,
        "area": area,
        "data_format": "grib",
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", required=True)
    p.add_argument("--region", choices=sorted(REGIONS))
    p.add_argument("--bbox", help="N/W/S/E bbox overriding --region")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    if not args.bbox and not args.region:
        print("Error: one of --region or --bbox is required.", file=sys.stderr)
        sys.exit(2)
    if args.bbox:
        area = [float(x) for x in args.bbox.split("/")]
    else:
        area = REGIONS[args.region]

    inputs = {"date": args.date, "area": area}
    out = Path(args.output)
    if _cache_hit(out, inputs):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    _require_env()

    import xarray as xr
    from ecmwf.datastores import Client

    print(f"Fetching ECMWF S2S for area={area} date={args.date}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="ecmwf-fetch-") as tmpdir:
        tmp = Path(tmpdir)
        cf_grib = tmp / "cf.grib"
        pf_grib = tmp / "pf.grib"

        client = Client()
        cf_req = _build_request(args.date, area, "control_forecast")
        pf_req = _build_request(args.date, area, "perturbed_forecast")

        # Submit cf and pf in parallel; ECDS retrievals are typically minutes to ~hour.
        print("Submitting cf and pf retrievals...", file=sys.stderr)
        cf_remote = _submit(client, cf_req)
        pf_remote = _submit(client, pf_req)
        remotes = [cf_remote, pf_remote]
        while not all(r.results_ready for r in remotes):
            time.sleep(30)

        print("Downloading cf...", file=sys.stderr)
        cf_remote.download(str(cf_grib))
        print("Downloading pf...", file=sys.stderr)
        pf_remote.download(str(pf_grib))

        print("Decoding GRIB and writing Zarr...", file=sys.stderr)
        cf = xr.open_dataset(cf_grib, engine="cfgrib").assign_coords(number=0)
        pf = xr.open_dataset(pf_grib, engine="cfgrib")
        ds = xr.concat([pf, cf], dim="number").sortby("number")
        ds.attrs.update(
            rhiza_source="ecmwf-s2s",
            rhiza_region=args.region or "",
            rhiza_area_NWSE="/".join(str(x) for x in area),
            rhiza_date=args.date,
            rhiza_inputs=json.dumps(inputs, sort_keys=True),
        )
        _stamp_cf_attrs(ds)
        # Stamp explicit units on tp so downstream consumers don't have to reverse-engineer
        # them from value ranges. GRIB carries `kg m**-2` (numerically equivalent to mm depth
        # over the accumulation period); we forward that exact string rather than convert.
        ds["tp"].attrs["units"] = "kg m**-2"
        ds["tp"].attrs["long_name"] = "Total precipitation"
        for v in ds.variables:
            ds[v].encoding = {}

        if out.exists():
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        ds.to_zarr(out, mode="w", consolidated=True)

    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
