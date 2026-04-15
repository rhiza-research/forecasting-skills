# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "tahmo",
# ]
#
# [tool.uv.sources]
# tahmo = { git = "https://github.com/rhiza-research/tahmo-api" }
# ///
"""Fetch TAHMO station observations and write a station-schema Rhiza Envelope Zarr.

Uses the TAHMO Python SDK directly. Credentials come from the environment:
TAHMO_API_USERNAME and TAHMO_API_PASSWORD.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path


COUNTRY_CODE = {
    "Burkina Faso": "BF",
    "Benin": "BJ",
    "DR Congo": "CD",
    "Côte d'Ivoire": "CI",
    "Cameroon": "CM",
    "Ethiopia": "ET",
    "Ghana": "GH",
    "Lesotho": "LS",
    "Madagascar": "MG",
    "Mali": "ML",
    "Malawi": "MW",
    "Mozambique": "MZ",
    "Niger": "NE",
    "Nigeria": "NG",
    "Rwanda": "RW",
    "Senegal": "SN",
    "Chad": "TD",
    "Togo": "TG",
    "Tanzania": "TZ",
    "Uganda": "UG",
    "South Africa": "ZA",
    "Zambia": "ZM",
    "Zimbabwe": "ZW",
    "Kenya": "KE",
}

# TAHMO short codes -> canonical variable names used in the envelope.
VAR_MAP = {
    "pr": "precip",
    "te": "temperature",
    "rh": "humidity",
    "ap": "pressure",
}
# How each variable aggregates from sub-daily to daily.
DAILY_AGG = {
    "precip": "sum",
    "temperature": "mean",
    "humidity": "mean",
    "pressure": "mean",
}


def _require_env() -> tuple[str, str]:
    u = os.environ.get("TAHMO_API_USERNAME")
    p = os.environ.get("TAHMO_API_PASSWORD")
    if not u or not p:
        print(
            "Error: TAHMO_API_USERNAME and TAHMO_API_PASSWORD must be set.",
            file=sys.stderr,
        )
        sys.exit(2)
    return u, p


def _station_frame(api, station_id: str, start: str, end: str):
    """Return a daily-aggregated DataFrame for one station, or None."""
    import pandas as pd

    try:
        raw = api.getRawData(
            station=station_id, startDate=start, endDate=end, dataset="controlled"
        )
    except Exception as exc:
        print(f"{station_id}: skipped ({exc})", file=sys.stderr)
        return None
    if raw is None or len(raw) == 0:
        return None

    raw["time"] = pd.to_datetime(raw["time"], format="mixed", utc=True).dt.tz_convert(
        None
    )
    keep_vars = set(VAR_MAP.keys())
    raw = raw[raw["variable"].isin(keep_vars)]
    if "quality" in raw.columns:
        raw = raw[raw["quality"] <= 2]
    if raw.empty:
        return None

    # For each (time, variable) pick the best-quality sensor (lowest quality flag).
    raw = raw.sort_values(["time", "variable", "quality"])
    raw = raw.drop_duplicates(["time", "variable"], keep="first")
    wide = raw.pivot(index="time", columns="variable", values="value")
    wide = wide.rename(columns=VAR_MAP)

    agg_spec = {c: DAILY_AGG[c] for c in wide.columns if c in DAILY_AGG}
    if not agg_spec:
        return None
    daily = wide.resample("D").agg(agg_spec)
    daily["station_id"] = station_id
    return daily


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--country", required=True, help="Comma-separated country names")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    username, password = _require_env()

    try:
        from TAHMO import apiWrapper
    except ImportError as exc:
        print(
            f"Error: could not import TAHMO ({exc}). Install via "
            f"'pip install git+https://github.com/rhiza-research/tahmo-api'.",
            file=sys.stderr,
        )
        sys.exit(2)

    import pandas as pd
    import xarray as xr

    countries = [c.strip() for c in args.country.split(",")]
    unknown = [c for c in countries if c not in COUNTRY_CODE]
    if unknown:
        print(
            f"Error: unknown countries {unknown}. Known: {sorted(COUNTRY_CODE)}",
            file=sys.stderr,
        )
        sys.exit(2)

    api = apiWrapper()
    api.setCredentials(username, password)
    stations_raw = api.getStations()
    stations = pd.json_normalize(list(stations_raw.values()), sep="_")

    frames = []
    meta_rows = []
    for country in countries:
        code = COUNTRY_CODE[country]
        sub = stations[stations["location_countrycode"] == code]
        sub = sub[sub["code"].str.startswith("TA")]
        if sub.empty:
            print(f"{country}: no stations", file=sys.stderr)
            continue
        for _, row in sub.iterrows():
            sid = row["code"]
            daily = _station_frame(api, sid, args.start, args.end)
            if daily is None:
                continue
            frames.append(daily)
            meta_rows.append(
                {
                    "station_id": sid,
                    "latitude": float(row["location_latitude"]),
                    "longitude": float(row["location_longitude"]),
                    "country": country,
                }
            )
            print(f"{country} {sid}: {len(daily)} daily rows", file=sys.stderr)

    if not frames:
        print("Error: no data returned for any station.", file=sys.stderr)
        sys.exit(1)

    df = pd.concat(frames).reset_index()
    meta = pd.DataFrame(meta_rows).drop_duplicates("station_id").set_index("station_id")
    df = df.set_index(["time", "station_id"])

    ds = xr.Dataset.from_dataframe(df)
    ds = ds.assign_coords(
        latitude=("station_id", meta.loc[ds["station_id"].values, "latitude"].values),
        longitude=("station_id", meta.loc[ds["station_id"].values, "longitude"].values),
        country=("station_id", meta.loc[ds["station_id"].values, "country"].values),
    )
    ds.attrs.update(
        rhiza_source="tahmo",
        rhiza_date=args.end,
        rhiza_region=",".join(countries),
    )
    for v in ds.variables:
        ds[v].encoding = {}

    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({dict(ds.sizes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
