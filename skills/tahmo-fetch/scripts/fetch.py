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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.2"

# Default size of the per-station fetch thread pool. The work is
# network-I/O-bound (one independent authenticated HTTP request per station),
# so threads overlap request latency without contending on the GIL. The bound
# is deliberately conservative: TAHMO's datahub is a research API and excessive
# concurrency risks 429/throttling. 8 is enough to hide request latency while
# staying well below levels that typically provoke rate limiting; operators can
# lower it with --workers if they observe throttling.
DEFAULT_WORKERS = 8

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


def _is_transient(exc: Exception) -> bool:
    """Heuristic: does this error look like a retryable transient/rate-limit?

    The TAHMO wrapper raises bare Exceptions whose message carries the HTTP
    status (e.g. "API request failed with status code 429"), plus requests/
    urllib3 connection and timeout errors. Match those so a momentary 429/5xx
    or dropped connection is retried rather than silently dropping a station.
    """
    text = str(exc).lower()
    transient_markers = ("429", "500", "502", "503", "504", "timed out", "timeout", "connection")
    return any(marker in text for marker in transient_markers)


def _fetch_raw(api, station_id: str, start: str, end: str):
    """Call getRawData once, retrying a single transient error after a short
    backoff. Returns the raw DataFrame (or None for genuine no-data). Raises if
    the error is non-transient or if the retry also fails transiently."""
    try:
        return api.getRawData(
            station=station_id, startDate=start, endDate=end, dataset="controlled"
        )
    except Exception as exc:
        if not _is_transient(exc):
            raise
        print(
            f"{station_id}: transient error ({exc}); retrying once",
            file=sys.stderr,
        )
        time.sleep(2.0)
        return api.getRawData(
            station=station_id, startDate=start, endDate=end, dataset="controlled"
        )


def _station_frame(api, station_id: str, start: str, end: str):
    """Return a daily-aggregated DataFrame for one station, or None.

    None means "no usable data for this station" (empty response, no kept
    variables, etc.) and is a quiet skip. A fetch that errors out — including a
    transient error that survives one retry — is logged distinctly on stderr as
    a dropped station so it is never silently lost, then returns None.
    """
    import pandas as pd

    try:
        raw = _fetch_raw(api, station_id, start, end)
    except Exception as exc:
        print(f"{station_id}: DROPPED, fetch failed ({exc})", file=sys.stderr)
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


def _load_history(zarr_path: Path) -> list:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            raw = ds.attrs.get("rhiza_history")
    except FileNotFoundError:
        # A not-yet-existing output read during a cache check is a silent miss.
        return []
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list):
        # A present-but-non-array value is malformed under the rhiza_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed rhiza_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


def _cache_hit(out: Path, entry: dict) -> bool:
    """Return True if the zarr at `out` was produced by this same entry."""
    if not out.exists():
        return False
    history = _load_history(out)
    if not history:
        return False
    existing_entry = history[0]
    return (
        existing_entry.get("skill") == entry["skill"]
        and existing_entry.get("version") == entry["version"]
        and existing_entry.get("args") == entry["args"]
        and existing_entry.get("input") == entry["input"]
    )


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
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            f"Max concurrent per-station fetch threads (default {DEFAULT_WORKERS}). "
            "Lower this if TAHMO returns 429/throttling errors."
        ),
    )
    args = p.parse_args()

    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        sys.exit(2)

    entry = {
        "skill": "tahmo-fetch",
        "version": _RHIZA_SKILL_VERSION,
        # Sort --country so that `--country Ghana --country Kenya` and
        # `--country Kenya --country Ghana` produce identical cache keys.
        # --workers is a concurrency knob, not a data parameter, so it is
        # excluded from the cache key: the same request at any worker count
        # produces the same data.
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output", "workers"}}
        | {"country": sorted(args.country)},
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
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

    # Flatten the selection into one task list of (country, station-row) pairs
    # across all requested countries, then fetch them concurrently. A single
    # bounded pool over the flat list keeps all worker slots busy regardless of
    # how stations are distributed between countries.
    tasks = []
    for country in countries:
        code = COUNTRY_CODE[country]
        sub = stations[stations["location_countrycode"] == code]
        sub = sub[sub["code"].str.startswith("TA")]
        if sub.empty:
            print(f"{country}: no stations", file=sys.stderr)
            continue
        for _, row in sub.iterrows():
            tasks.append((country, row))

    def _fetch_one(country, row):
        """Worker: fetch one station and return its paired (frame, meta_row).

        The frame and meta_row are produced together so that whatever order
        futures complete in, each station's data stays bound to its own
        latitude/longitude/country. Returns None when the station has no usable
        data (or was dropped after a failed fetch) so it can be skipped.

        The single `api` instance is shared across workers. The TAHMO
        apiWrapper carries no per-call mutable state: setCredentials sets
        apiKey/apiSecret once, and getRawData/__request only read them, build a
        fresh requests.get per request (no shared Session), and keep all
        intermediate state in locals. Concurrent getRawData calls on one
        instance therefore do not race on shared state.
        """
        sid = row["code"]
        daily = _station_frame(api, sid, args.start, args.end)
        if daily is None:
            return None
        meta_row = {
            "station_id": sid,
            "latitude": float(row["location_latitude"]),
            "longitude": float(row["location_longitude"]),
            "country": country,
        }
        return daily, meta_row

    frames = []
    meta_rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_fetch_one, country, row): (country, row["code"]) for country, row in tasks
        }
        for fut in as_completed(futures):
            country, sid = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001 -- isolate per-station failures
                # A worker that raises (e.g. a station row with a null/NaN
                # coordinate, or any other unexpected processing error) drops
                # only that station rather than aborting the whole run.
                print(f"{country} {sid}: DROPPED, worker error ({exc})", file=sys.stderr)
                continue
            if result is None:
                continue
            daily, meta_row = result
            frames.append(daily)
            meta_rows.append(meta_row)
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
        rhiza_history=json.dumps([entry], sort_keys=True),
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
