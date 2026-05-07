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
import json
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
# CF metadata per envelope variable as (standard_name, units_override).
# Standard names are verified against the CF standard name table v93. Units
# are pulled live from api.getVariables() so they track whatever TAHMO is
# actually returning, except for `precip`: the raw TAHMO shortcode reports
# in "mm" per measurement, and our daily sum aggregation produces mm-per-day
# which is the rate label that pairs with lwe_precipitation_rate.
CF_META = {
    "precip": ("lwe_precipitation_rate", "mm/day"),
    "temperature": ("air_temperature", None),
    "humidity": ("relative_humidity", None),
    "pressure": ("air_pressure", None),
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
        raw = api.getRawData(station=station_id, startDate=start, endDate=end, dataset="controlled")
    except Exception as exc:
        print(f"{station_id}: skipped ({exc})", file=sys.stderr)
        return None
    if raw is None or len(raw) == 0:
        return None

    raw["time"] = pd.to_datetime(raw["time"], format="mixed", utc=True).dt.tz_convert(None)
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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--country",
        action="append",
        required=True,
        help="Country name (pass once per country)",
    )
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    inputs = {
        "country": sorted(args.country),
        "start": args.start,
        "end": args.end,
    }
    out = Path(args.output)
    if _cache_hit(out, inputs):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

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

    countries = list(args.country)
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
    # Discover units / description from TAHMO so we don't hard-code them.
    var_meta = api.getVariables()

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
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")
    ds["station_id"].attrs.update(cf_role="timeseries_id", long_name="TAHMO station identifier")
    ds["country"].attrs.update(long_name="country name")
    short_code_for = {v: k for k, v in VAR_MAP.items()}
    for canonical in ds.data_vars:
        short = short_code_for.get(canonical)
        api_meta = var_meta.get(short, {}) if short else {}
        std_name, units_override = CF_META.get(canonical, (None, None))
        attrs = {}
        if std_name:
            attrs["standard_name"] = std_name
        units = units_override or api_meta.get("units")
        if units:
            attrs["units"] = units
        description = api_meta.get("description")
        if description:
            attrs["long_name"] = description
        if attrs:
            ds[canonical].attrs.update(attrs)
    ds.attrs.update(
        rhiza_source="tahmo",
        rhiza_date=args.end,
        rhiza_region=",".join(countries),
        rhiza_inputs=json.dumps(inputs, sort_keys=True),
        featureType="timeSeries",
    )
    for v in ds.variables:
        ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({dict(ds.sizes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
