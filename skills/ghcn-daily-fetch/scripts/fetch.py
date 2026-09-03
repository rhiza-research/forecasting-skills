# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "requests",
#   "cf_xarray",
#   "pint-xarray>=0.6",
#   "cftime",
# ]
# ///
"""Fetch NOAA GHCN-Daily station observations over HTTPS and write a point_obs weather-skills standard dataset Zarr."""

import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import cf_xarray  # noqa: F401  (fail-fast probe; core loads lazily at write time)
import cf_xarray.units  # noqa: F401  (fail-fast probe; configures pint CF registry)
import numpy as np
import pandas as pd
import requests
import xarray as xr
from weather_skills_core import DataError, weather_skill
from weather_skills_core.cf import stamp_cf_dsg, udunits_error, verify_cf_dsg
from weather_skills_core.standard_utils import apply_write_encoding, is_transient
from weather_skills_core.units import precip_amounts_to_rates, stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_BASE_URL = "https://noaa-ghcn-pds.s3.amazonaws.com"
_STATIONS_URL = f"{_BASE_URL}/ghcnd-stations.txt"
_STATION_CSV_URL = _BASE_URL + "/csv.gz/by_station/{station_id}.csv.gz"
_YEAR_CSV_URL = _BASE_URL + "/csv.gz/by_year/{year}.csv.gz"
_CSV_COLUMNS = ["ID", "DATE", "ELEMENT", "VALUE", "M_FLAG", "Q_FLAG", "S_FLAG", "OBS_TIME"]
_GHCN_MISSING_VALUE = -9999
HTTP_TIMEOUT = 60
DEFAULT_WORKERS = 8

_TIME_UNITS = "days since 1970-01-01"
_TIME_CALENDAR = "proleptic_gregorian"

# canonical -> (GHCN element, scale, units, standard_name, cell_method, long_name).
# Scale converts tenths-of-mm / tenths-of-degC to mm day-1 and degree_Celsius.
# Precip is stamped as a daily rate (no cell_methods sum — that marks convert-to-totals).
VAR_MAP = {
    "precip": ("PRCP", 0.1, "mm day-1", "lwe_precipitation_rate", None, "daily precipitation rate"),
    "tmax": (
        "TMAX",
        0.1,
        "degree_Celsius",
        "air_temperature",
        "time: maximum",
        "daily maximum air temperature",
    ),
    "tmin": (
        "TMIN",
        0.1,
        "degree_Celsius",
        "air_temperature",
        "time: minimum",
        "daily minimum air temperature",
    ),
    "tavg": (
        "TAVG",
        0.1,
        "degree_Celsius",
        "air_temperature",
        "time: mean",
        "daily mean air temperature",
    ),
}
DEFAULT_VARIABLES = ["precip", "tmax", "tmin"]


def _load_stations(bbox):
    """Fetch ghcnd-stations.txt, optionally filtered to a bbox."""
    resp = requests.get(_STATIONS_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    stations = pd.read_fwf(
        io.StringIO(resp.text),
        colspecs=[(0, 11), (12, 20), (21, 30), (41, 71)],
        names=["station_id", "latitude", "longitude", "name"],
    )
    stations["name"] = stations["name"].fillna("").astype(str).str.strip()
    stations = stations.dropna(subset=["latitude", "longitude"])
    if bbox is not None:
        north, west, south, east = bbox
        lat_in = (stations["latitude"] >= south) & (stations["latitude"] <= north)
        if west <= east:
            lon_in = (stations["longitude"] >= west) & (stations["longitude"] <= east)
        else:
            lon_in = (stations["longitude"] >= west) | (stations["longitude"] <= east)
        stations = stations[lat_in & lon_in]
    return stations.set_index("station_id")


def _fetch_station_csv(station_id: str):
    """GET one station's gzip CSV; retry once on transient error. None on 404."""
    url = _STATION_CSV_URL.format(station_id=station_id)
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            if attempt == 0 and is_transient(exc):
                time.sleep(2.0)
                continue
            raise
    return None


def _station_frame(station_id: str, elements: dict, start_int: int, end_int: int):
    """Daily DataFrame for one station within the window, or None."""
    content = _fetch_station_csv(station_id)
    if content is None:
        return None
    raw = pd.read_csv(
        io.BytesIO(content),
        compression="gzip",
        header=None,
        names=_CSV_COLUMNS,
        dtype={
            "ID": str,
            "ELEMENT": str,
            "M_FLAG": str,
            "Q_FLAG": str,
            "S_FLAG": str,
            "OBS_TIME": str,
        },
    )
    raw = raw[raw["ELEMENT"].isin(elements)]
    raw = raw[(raw["DATE"] >= start_int) & (raw["DATE"] <= end_int)]
    raw = raw[raw["Q_FLAG"].isna()]
    raw = raw[raw["VALUE"] != _GHCN_MISSING_VALUE]
    if raw.empty:
        return None

    raw["time"] = pd.to_datetime(raw["DATE"], format="%Y%m%d")
    wide = raw.pivot_table(index="time", columns="ELEMENT", values="VALUE", aggfunc="first")
    out_cols = {}
    for element, canonical in elements.items():
        if element in wide.columns:
            out_cols[canonical] = wide[element] * VAR_MAP[canonical][1]
    if not out_cols:
        return None
    daily = pd.DataFrame(out_cols)
    daily["station_id"] = station_id
    return daily


def _var_attrs(ds) -> dict:
    attrs = {}
    for canonical in ds.data_vars:
        _element, _scale, units, std_name, cell_method, long_name = VAR_MAP[canonical]
        exc = udunits_error(units)
        if exc is not None:
            raise DataError(
                f"units {units!r} for variable {canonical!r} are not udunits-valid "
                f"({exc}); refusing to write a non-CF store under a CF-1.13 claim."
            )
        attrs[canonical] = {
            "standard_name": std_name,
            "long_name": long_name,
            "units": units,
        }
        if cell_method:
            attrs[canonical]["cell_methods"] = cell_method
    return attrs


@weather_skill(
    name="ghcn-daily-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=(
        f"Max concurrent per-station download threads (default {DEFAULT_WORKERS}). "
        "Lower this if the server returns throttling errors."
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
def fetch(start_time, end_time, bbox, workers, variable, **kwargs):
    """Fetch NOAA GHCN-Daily station observations over HTTPS and write a point_obs weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        from email.utils import parsedate_to_datetime

        year = datetime.now(UTC).date().year
        for y in (year, year - 1):
            resp = requests.head(
                _YEAR_CSV_URL.format(year=y), timeout=HTTP_TIMEOUT, allow_redirects=True
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            last_modified = resp.headers.get("Last-Modified")
            if not last_modified:
                raise DataError(f"GHCN year file for {y} has no Last-Modified header")
            print(parsedate_to_datetime(last_modified).date().isoformat())
            return
        raise DataError("GHCN probe could not HEAD a by_year csv.gz")

    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    start_int = int(start_time.strftime("%Y%m%d"))
    end_int = int(end_time.strftime("%Y%m%d"))
    bbox_label = f"{bbox[0]}/{bbox[1]}/{bbox[2]}/{bbox[3]}" if bbox is not None else None

    variables = sorted(variable or list(DEFAULT_VARIABLES))
    elements = {VAR_MAP[v][0]: v for v in variables}

    stations = _load_stations(bbox)
    if stations.empty:
        where = (
            f"the requested --bbox {bbox_label}"
            if bbox_label is not None
            else "the GHCN-Daily station table"
        )
        raise DataError(f"no stations in {where}.")

    candidate_count = len(stations)
    print(
        f"Fetching {candidate_count} candidate stations for {start_iso}..{end_iso}",
        file=sys.stderr,
    )

    frames = []
    meta_rows = []
    dropped = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_station_frame, sid, elements, start_int, end_int): sid
            for sid in stations.index
        }
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                daily = fut.result()
            except Exception as exc:  # noqa: BLE001
                dropped += 1
                print(f"{sid}: DROPPED, fetch failed ({exc})", file=sys.stderr)
                continue
            if daily is None:
                continue
            row = stations.loc[sid]
            meta_rows.append(
                {
                    "station_id": sid,
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "name": str(row["name"]),
                }
            )
            frames.append(daily)

    if dropped:
        print(
            f"Dropped {dropped} of {candidate_count} candidate stations after a "
            "failed fetch + one retry.",
            file=sys.stderr,
        )

    if not frames:
        where = (
            f"within the requested --bbox {bbox_label}"
            if bbox_label is not None
            else "across the GHCN-Daily station network"
        )
        raise DataError(
            f"no GHCN-Daily observations for {sorted(variables)} in {start_iso}..{end_iso} {where}."
        )

    df = pd.concat(frames).reset_index()
    meta = pd.DataFrame(meta_rows).drop_duplicates("station_id").set_index("station_id")
    df = df.set_index(["time", "station_id"])

    ds = xr.Dataset.from_dataframe(df)
    ds = ds.assign_coords(
        latitude=("station_id", meta.loc[ds["station_id"].values, "latitude"].values),
        longitude=("station_id", meta.loc[ds["station_id"].values, "longitude"].values),
        name=("station_id", meta.loc[ds["station_id"].values, "name"].values),
    )

    missing_vars = [v for v in variables if v not in ds.data_vars]
    if missing_vars:
        print(
            f"Warning: requested variable(s) {sorted(missing_vars)} were reported by no "
            f"station in the selection for {start_iso}..{end_iso}; they are omitted from "
            "the output.",
            file=sys.stderr,
        )

    stamp_cf_dsg(
        ds,
        _var_attrs(ds),
        station_id_long_name="GHCN station identifier",
        name_long_name="station name",
    )
    ds.attrs.update(
        Conventions="CF-1.13",
        featureType="timeSeries",
        title="NOAA GHCN-Daily station observations",
        source="NOAA Global Historical Climatology Network - Daily (GHCN-Daily)",
        institution="NOAA National Centers for Environmental Information",
        references="https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily",
        history=f"{datetime.now(UTC).isoformat()} ghcn-daily-fetch {start_iso}..{end_iso}",
        weather_skills_source="ghcn-daily",
    )

    verify_cf_dsg(ds)
    apply_write_encoding(
        ds,
        time_units=_TIME_UNITS,
        time_calendar=_TIME_CALENDAR,
        fills={v: np.float64(np.nan) for v in ds.data_vars},
    )
    ds = precip_amounts_to_rates(ds, interval="1 day")
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()
