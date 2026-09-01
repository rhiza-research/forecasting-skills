# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "tahmo",
# ]
#
# [tool.uv.sources]
# tahmo = { git = "https://github.com/rhiza-research/tahmo-api", rev = "8ed3adc22b5b7c53d08753e45676e9d4a0a52ab8" }
# ///
"""Fetch TAHMO station observations and write a point_obs weather-skills standard dataset Zarr.

Uses the TAHMO Python SDK. Credentials: TAHMO_API_USERNAME and TAHMO_API_PASSWORD.

List stations in a region from the deployment API (no ``-o``)::

    tahmo-fetch --list-stations --bbox N/W/S/E

Then fetch chosen station IDs (and/or every TA station in a bbox).
"""

import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import udunits_error
from weather_skills_core.standard_utils import is_transient, require_env
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

DEFAULT_WORKERS = 8
_TAHMO_PREFIX = "TA"

VAR_MAP = {
    "pr": "precip",
    "te": "temperature",
    "rh": "humidity",
    "ap": "pressure",
}
DAILY_AGG = {
    "precip": "sum",
    "temperature": "mean",
    "humidity": "mean",
    "pressure": "mean",
}
# (standard_name, units). Always CF/pint strings — TAHMO getVariables() uses
# "-" for dimensionless (relative humidity) and that fails pint.quantify().
CF_META = {
    "precip": ("lwe_precipitation_rate", "mm day-1"),
    "temperature": ("air_temperature", "degree_Celsius"),
    "humidity": ("relative_humidity", "1"),
    "pressure": ("air_pressure", "kPa"),
}
# TAHMO API leftovers if CF_META has no override.
_TAHMO_UNITS = {
    "-": "1",
    "degrees Celsius": "degree_Celsius",
    "degree Celsius": "degree_Celsius",
}


def _pint_units(raw) -> str | None:
    """Map a TAHMO/API units string to a pint-parseable CF spelling, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    return _TAHMO_UNITS.get(text, text)


def _coord_str(row, *keys) -> str:
    for key in keys:
        if key not in row.index:
            continue
        val = row[key]
        if val is None or val != val:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _station_name(row) -> str:
    return _coord_str(row, "location_name", "name")


def _ta_stations(stations):
    out = stations.dropna(subset=["location_latitude", "location_longitude"])
    return out[out["code"].astype(str).str.startswith(_TAHMO_PREFIX)].copy()


def _filter_bbox(stations, bbox):
    north, west, south, east = bbox
    lat = stations["location_latitude"]
    lon = stations["location_longitude"]
    lat_in = (lat >= south) & (lat <= north)
    if west <= east:
        lon_in = (lon >= west) & (lon <= east)
    else:
        lon_in = (lon >= west) | (lon <= east)
    return stations.loc[lat_in & lon_in].copy()


def _print_station_list(stations) -> None:
    ordered = stations.sort_values("code")
    writer = csv.writer(sys.stdout, dialect="excel-tab", lineterminator="\n")
    writer.writerow(["station_id", "name", "latitude", "longitude", "country"])
    for _, row in ordered.iterrows():
        writer.writerow(
            [
                str(row["code"]),
                _station_name(row),
                f"{float(row['location_latitude']):.5f}",
                f"{float(row['location_longitude']):.5f}",
                _coord_str(row, "location_countrycode"),
            ]
        )
    print(f"{len(ordered)} stations", file=sys.stderr)


def _select_stations(stations, station_ids, bbox):
    """Return deployment rows for explicit ``--station`` IDs, else TA stations in ``--bbox``."""
    if station_ids:
        wanted = [s.strip().upper() for s in station_ids]
        by_code = stations.set_index(stations["code"].astype(str).str.upper())
        missing = [s for s in wanted if s not in by_code.index]
        if missing:
            raise UsageError("unknown TAHMO station id(s): " + ", ".join(missing))
        return by_code.loc[wanted].reset_index(drop=True)

    if bbox is None:
        raise UsageError("pass --station ID (repeatable) and/or --bbox N/W/S/E.")

    selected = _filter_bbox(_ta_stations(stations), bbox)
    if selected.empty:
        north, west, south, east = bbox
        raise DataError(
            f"no TAHMO stations in --bbox {north:g}/{west:g}/{south:g}/{east:g}."
        )
    return selected


def _fetch_raw(api, station_id: str, start: str, end: str):
    """Call getRawData once, retrying a single transient error after a short backoff."""
    try:
        return api.getRawData(
            station=station_id, startDate=start, endDate=end, dataset="controlled"
        )
    except Exception as exc:
        if not is_transient(exc):
            raise
        print(f"{station_id}: transient error ({exc}); retrying once", file=sys.stderr)
        time.sleep(2.0)
        return api.getRawData(
            station=station_id, startDate=start, endDate=end, dataset="controlled"
        )


def _station_frame(api, station_id: str, start: str, end: str):
    """Return a daily-aggregated DataFrame for one station, or None."""
    import pandas as pd

    try:
        raw = _fetch_raw(api, station_id, start, end)
    except Exception as exc:  # noqa: BLE001
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


def _ensure_setup(state):
    """Authenticate and load the deployment catalogue + variable metadata, at most once."""
    import pandas as pd

    if "api" not in state:
        username, password = require_env(
            "TAHMO_API_USERNAME",
            "TAHMO_API_PASSWORD",
            message="TAHMO_API_USERNAME and TAHMO_API_PASSWORD must be set.",
        )
        try:
            from TAHMO import apiWrapper
        except ImportError as exc:
            raise UsageError(
                f"could not import TAHMO ({exc}). Install via "
                f"'pip install git+https://github.com/rhiza-research/tahmo-api'."
            ) from None
        api_local = apiWrapper()
        api_local.setCredentials(username, password)
        stations_raw = api_local.getStations()
        stations_local = pd.json_normalize(list(stations_raw.values()), sep="_")
        state["api"] = api_local
        state["stations"] = stations_local
        state["var_meta"] = api_local.getVariables()
    return state["api"], state["stations"], state["var_meta"]


@weather_skill(
    name="tahmo-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--bbox",
    help=(
        "Select every TA station in this box. With --list-stations, the region "
        "to search on the TAHMO deployment API. Ignored for selection when "
        "--station IDs are given."
    ),
)
@weather_skill.argument(
    "--station",
    action="append",
    help="TAHMO station id (repeatable), e.g. TA00025. Discover ids with --list-stations.",
)
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=(
        f"Max concurrent per-station fetch threads (default {DEFAULT_WORKERS}). "
        "Lower this if TAHMO returns 429/throttling errors."
    ),
)
@weather_skill.argument(
    "--list-stations",
    action="store_true",
    default=False,
    probe=True,
    help=(
        "Print TA stations in --bbox from the TAHMO deployment API as TSV on "
        "stdout (station_id, name, latitude, longitude, country) and exit. No -o."
    ),
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
def fetch(start_time, end_time, workers, bbox, station, **kwargs):
    """Fetch TAHMO station observations and write a point_obs weather-skills standard dataset Zarr."""
    import pandas as pd
    import xarray as xr

    list_stations = kwargs.get("list_stations")
    if list_stations and bbox is None:
        raise UsageError("--list-stations requires --bbox N/W/S/E.")
    if (
        not list_stations
        and kwargs.get("probe_latest") is None
        and not station
        and bbox is None
    ):
        raise UsageError("pass --station ID (repeatable) and/or --bbox N/W/S/E.")

    api, stations, var_meta = _ensure_setup({})

    if list_stations:
        _print_station_list(_filter_bbox(_ta_stations(stations), bbox))
        return

    if kwargs.get("probe_latest") is not None:
        from datetime import UTC, datetime, timedelta

        end = datetime.now(UTC).date()
        start = end - timedelta(days=14)
        if station or bbox is not None:
            selected = _select_stations(stations, station, bbox)
        else:
            selected = _ta_stations(stations).head(3)
        latest = None
        for _, row in selected.iterrows():
            daily = _station_frame(api, row["code"], start.isoformat(), end.isoformat())
            if daily is None or daily.empty:
                continue
            day = daily.index.max().date()
            latest = day if latest is None else max(latest, day)
        if latest is None:
            raise DataError("TAHMO probe found no recent observations")
        print(latest.isoformat())
        return

    selected = _select_stations(stations, station, bbox)
    start = start_time.isoformat()
    end = end_time.isoformat()
    print(
        f"Fetching {len(selected)} TAHMO station(s) {start} → {end}",
        file=sys.stderr,
    )

    def _fetch_one(row):
        sid = row["code"]
        daily = _station_frame(api, sid, start, end)
        if daily is None:
            return None
        return daily, {
            "station_id": sid,
            "latitude": float(row["location_latitude"]),
            "longitude": float(row["location_longitude"]),
            "country": _coord_str(row, "location_countrycode"),
            "name": _station_name(row),
        }

    frames = []
    meta_rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, row): row["code"] for _, row in selected.iterrows()}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"{sid}: DROPPED, worker error ({exc})", file=sys.stderr)
                continue
            if result is None:
                continue
            daily, meta_row = result
            frames.append(daily)
            meta_rows.append(meta_row)
            print(f"{sid}: {len(daily)} daily rows", file=sys.stderr)

    if not frames:
        raise DataError("no data returned for any station.")

    df = pd.concat(frames).reset_index()
    meta = pd.DataFrame(meta_rows).drop_duplicates("station_id").set_index("station_id")
    df = df.set_index(["time", "station_id"])

    ds = xr.Dataset.from_dataframe(df)
    ids = ds["station_id"].values
    ds = ds.assign_coords(
        latitude=("station_id", meta.loc[ids, "latitude"].values),
        longitude=("station_id", meta.loc[ids, "longitude"].values),
        country=("station_id", meta.loc[ids, "country"].values),
        name=("station_id", meta.loc[ids, "name"].values),
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")
    ds["station_id"].attrs.update(cf_role="timeseries_id", long_name="TAHMO station identifier")
    ds["country"].attrs.update(long_name="ISO 3166-1 alpha-2 country code")
    ds["name"].attrs.update(long_name="station name")
    short_code_for = {v: k for k, v in VAR_MAP.items()}
    for canonical in ds.data_vars:
        short = short_code_for.get(canonical)
        api_meta = var_meta.get(short, {}) if short else {}
        std_name, units_override = CF_META.get(canonical, (None, None))
        attrs = {"coordinates": "latitude longitude"}
        if std_name:
            attrs["standard_name"] = std_name
        units = _pint_units(units_override) or _pint_units(api_meta.get("units"))
        if not units:
            raise DataError(
                f"variable {canonical!r} has no pint-parseable units from TAHMO metadata."
            )
        exc = udunits_error(units)
        if exc is not None:
            raise DataError(
                f"units {units!r} for variable {canonical!r} are not udunits-valid "
                f"({exc}); refusing to write a non-CF store."
            )
        attrs["units"] = units
        description = api_meta.get("description")
        if description:
            attrs["long_name"] = description
        ds[canonical].attrs.update(attrs)
    ds.attrs.update(
        featureType="timeSeries",
        Conventions="CF-1.13",
        weather_skills_source="tahmo",
    )
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()
