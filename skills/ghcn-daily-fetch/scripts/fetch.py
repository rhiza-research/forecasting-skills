# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@cursor/simplify-weather-skill-decorator",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "requests",
#   "cf_xarray",
#   "cf_units",
#   "cftime",
# ]
# ///
"""Fetch NOAA GHCN-Daily station observations over HTTPS and write a station-schema weather-skills envelope Zarr."""

import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

# cf_units and cf_xarray are used only inside weather-skills-core, which imports
# them lazily at write time (udunits_error / verify_cf_dsg) — the final step, after
# every per-station download. Importing them eagerly at module top turns that late
# import into a startup fail-fast probe: a missing dependency errors before any
# network work rather than only after every download has run. The F401 noqa marks
# these probe-only imports; removing either would drop the fail-fast guarantee.
import cf_units  # noqa: F401  (loaded lazily by core's udunits_error at write time)
import cf_xarray  # noqa: F401  (loaded lazily by core's verify_cf_dsg at write time)
import numpy as np
import pandas as pd
import requests
import xarray as xr
from weather_skills_core import DataError, weather_skill
from weather_skills_core.envelope import stamp_cf_dsg, udunits_error, verify_cf_dsg
from weather_skills_core.util import is_transient

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.7"

# Public, credential-free GHCN-Daily on the S3 website endpoint.
_BASE_URL = "https://noaa-ghcn-pds.s3.amazonaws.com"
_STATIONS_URL = f"{_BASE_URL}/ghcnd-stations.txt"
# Per-station CSV (gzip, NO header). Positional columns:
# ID, DATE(YYYYMMDD), ELEMENT, VALUE, M_FLAG, Q_FLAG, S_FLAG, OBS_TIME.
_STATION_CSV_URL = _BASE_URL + "/csv.gz/by_station/{station_id}.csv.gz"
_CSV_COLUMNS = ["ID", "DATE", "ELEMENT", "VALUE", "M_FLAG", "Q_FLAG", "S_FLAG", "OBS_TIME"]
# GHCN-Daily's missing-value sentinel for the raw integer VALUE field. It can
# appear with an empty Q_FLAG, so it is filtered explicitly (not caught by the QC
# filter) and dropped before unit scaling so a missing cell becomes NaN.
_GHCN_MISSING_VALUE = -9999
HTTP_TIMEOUT = 60
DEFAULT_WORKERS = 8

# CF time-coordinate encoding. udunits-valid reference-time units plus a
# calendar, carried in the write encoding so the on-disk time axis is fully CF.
_TIME_UNITS = "days since 1970-01-01"
_TIME_CALENDAR = "proleptic_gregorian"

# --- Source -> output transforms ---
# Everything not listed here is a verbatim pass-through from the raw GHCN-Daily
# source. The transforms applied below (all encoded in VAR_MAP and the per-value
# scaling in _station_frame) are:
#
# Value conversions (scale 0.1, applied in _station_frame as wide[element] *
# scale):
#   - PRCP: raw integer "tenths of a mm" -> mm/day
#   - TMAX/TMIN/TAVG: raw integer "tenths of a degree C" -> degC
#   There is no verbatim pass-through of the raw values because GHCN's "tenths"
#   integer is a documented storage encoding with no udunits unit of its own; a
#   conversion to whole mm / degC is required. This is a value conversion, not a
#   unit remap.
#
# Variable renames (GHCN element code -> canonical envelope variable name, from
# the VAR_MAP keys/element fields):
#   - PRCP -> precip
#   - TMAX -> tmax
#   - TMIN -> tmin
#   - TAVG -> tavg
#
# Metadata assignment basis (per variable, from VAR_MAP):
#   - units: the udunits-valid string in VAR_MAP (precip "mm/day"; tmax/tmin/tavg
#     "degC"), validated at write time via weather_skills_core's udunits_error
#     (in _var_attrs).
#   - standard_name: the CF standard name in VAR_MAP (precip
#     "lwe_precipitation_rate"; tmax/tmin/tavg "air_temperature").
#   - long_name: the descriptive string in VAR_MAP (precip "daily total
#     precipitation"; tmax "daily maximum air temperature"; tmin "daily minimum
#     air temperature"; tavg "daily mean air temperature").
#
# Canonical envelope variable -> (GHCN element, scale, units, standard_name,
# cell_method, long_name).
# GHCN stores PRCP in tenths of mm and TMAX/TMIN/TAVG in tenths of degrees C, so
# the scale brings raw integers to mm/day and degrees C respectively. `units`
# strings are udunits-valid (validated at write time); `standard_name` values
# are from the CF standard name table; `cell_methods` records the within-day
# reduction GHCN applies to each element.
VAR_MAP = {
    "precip": (
        "PRCP",
        0.1,
        "mm/day",
        "lwe_precipitation_rate",
        "time: sum",
        "daily total precipitation",
    ),
    "tmax": (
        "TMAX",
        0.1,
        "degC",
        "air_temperature",
        "time: maximum",
        "daily maximum air temperature",
    ),
    "tmin": (
        "TMIN",
        0.1,
        "degC",
        "air_temperature",
        "time: minimum",
        "daily minimum air temperature",
    ),
    "tavg": (
        "TAVG",
        0.1,
        "degC",
        "air_temperature",
        "time: mean",
        "daily mean air temperature",
    ),
}
DEFAULT_VARIABLES = ["precip", "tmax", "tmin"]

def _load_stations(bbox):
    """Fetch and parse ghcnd-stations.txt, optionally filtered to a bbox.

    Returns a DataFrame indexed by station ID with latitude/longitude/name.
    ``bbox`` is a parsed (north, west, south, east) tuple, or None.
    """
    resp = requests.get(_STATIONS_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    # Fixed-width per the GHCN-Daily readme: ID 1-11, LAT 13-20, LON 22-30,
    # NAME 42-71 (1-indexed inclusive -> 0-indexed half-open colspecs below).
    stations = pd.read_fwf(
        io.StringIO(resp.text),
        colspecs=[(0, 11), (12, 20), (21, 30), (41, 71)],
        names=["station_id", "latitude", "longitude", "name"],
    )
    # A missing NAME field parses as NaN; render it as an empty string rather
    # than the literal "nan" that str(NaN) would produce.
    stations["name"] = stations["name"].fillna("").astype(str).str.strip()
    # A malformed or short fixed-width line yields NaN latitude/longitude; drop
    # those so a station with no usable position never reaches the output (it
    # would otherwise be written with NaN coords on a no-bbox pull).
    stations = stations.dropna(subset=["latitude", "longitude"])
    if bbox is not None:
        north, west, south, east = bbox
        lat_in = (stations["latitude"] >= south) & (stations["latitude"] <= north)
        if west <= east:
            lon_in = (stations["longitude"] >= west) & (stations["longitude"] <= east)
        else:
            # Antimeridian-crossing bbox (west > east, e.g. 10/170/-10/-170):
            # the longitude span wraps across +/-180, so a station is inside when
            # its longitude is >= west OR <= east, not the AND (which would be
            # empty for every station).
            lon_in = (stations["longitude"] >= west) | (stations["longitude"] <= east)
        stations = stations[lat_in & lon_in]
    return stations.set_index("station_id")

def _fetch_station_csv(station_id: str):
    """GET one station's gzip CSV bytes, retrying once on a transient error.

    Returns the response content (bytes), or None on HTTP 404 (station has no
    by_station file). Raises on a non-transient error or a surviving transient.
    """
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
    """Return a daily DataFrame (time index, canonical-variable columns) for one
    station within the window, or None when it has no usable rows.

    `elements` maps GHCN element code -> canonical variable name.
    """
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
    # Empty Q_FLAG means the value passed every quality-control check; any flag
    # letter marks a failed check, so drop those rows.
    raw = raw[raw["Q_FLAG"].isna()]
    # GHCN-Daily encodes a missing observation as the raw integer sentinel -9999
    # (which can carry an empty Q_FLAG). Drop those rows BEFORE unit scaling so a
    # missing cell becomes NaN in the envelope rather than being scaled to a
    # spurious -999.9 real observation.
    raw = raw[raw["VALUE"] != _GHCN_MISSING_VALUE]
    if raw.empty:
        return None

    raw["time"] = pd.to_datetime(raw["DATE"], format="%Y%m%d")
    wide = raw.pivot_table(index="time", columns="ELEMENT", values="VALUE", aggfunc="first")
    out_cols = {}
    for element, canonical in elements.items():
        if element in wide.columns:
            scale = VAR_MAP[canonical][1]
            out_cols[canonical] = wide[element] * scale
    if not out_cols:
        return None
    daily = pd.DataFrame(out_cols)
    daily["station_id"] = station_id
    return daily

def _var_attrs(ds) -> dict:
    """Per-variable CF attr dicts from VAR_MAP, udunits-validated.

    A CF data variable's `units` must parse under udunits; emitting an
    unparseable string while claiming CF compliance is a false claim. cf_units
    wraps the same udunits-2 library cf-checker uses, so this is a real check,
    not a regex. Missing station-time cells are represented by `_FillValue`,
    set as an encoding key (not an attribute) by the write-encoding hook; a NaN
    `missing_value` attribute is not added — it is redundant with the NaN
    `_FillValue`, and xarray's CF encoder drops a NaN `missing_value` on write
    rather than persist it, so claiming it as an attr would be false.
    """
    attrs = {}
    for canonical in ds.data_vars:
        _element, _scale, units, std_name, cell_method, long_name = VAR_MAP[canonical]
        exc = udunits_error(units)
        if exc is not None:
            raise DataError(
                f"units {units!r} for variable {canonical!r} are not udunits-valid "
                f"({exc}); refusing to write a non-CF store under a CF-1.13 claim. Fix the "
                "units in VAR_MAP."
            )
        attrs[canonical] = {
            "standard_name": std_name,
            "long_name": long_name,
            "units": units,
            "cell_methods": cell_method,
        }
    return attrs

def _set_write_encoding(ds) -> None:
    """Controlled write encodings, applied after the decorator's encoding clear.

    Carry udunits-valid reference-time units + a calendar in the time encoding
    so the on-disk time axis is fully CF. `_FillValue` is an encoding key, not
    a CF attribute: set here so the on-disk store represents missing
    station-time cells with a real fill (and reopens with the NaNs intact).
    """
    ds["time"].encoding["units"] = _TIME_UNITS
    ds["time"].encoding["calendar"] = _TIME_CALENDAR
    for canonical in ds.data_vars:
        ds[canonical].encoding["_FillValue"] = np.float64(np.nan)

@weather_skill(
    "ghcn-daily-fetch",
    _SKILL_VERSION,
    outputs=["station"]
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
def fetch(start_time, end_time, bbox, workers, variable, **kwargs):
    """Fetch NOAA GHCN-Daily station observations over HTTPS and write a station-schema weather-skills envelope Zarr."""
    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    start_int = int(start_time.strftime("%Y%m%d"))
    end_int = int(end_time.strftime("%Y%m%d"))
    bbox_label = (
        f"{bbox[0]}/{bbox[1]}/{bbox[2]}/{bbox[3]}" if bbox is not None else None
    )

    variables = sorted(variable or list(DEFAULT_VARIABLES))
    # element code -> canonical variable name, for the requested variables.
    elements = {VAR_MAP[v][0]: v for v in variables}

    # Parse + bbox-filter the (single, cheap) station metadata file before any
    # per-station download.
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
            except Exception as exc:  # noqa: BLE001 -- isolate per-station failures
                # A station whose fetch raised even after the single in-flight
                # retry is dropped (not silently lost): logged per-line here and
                # rolled into the aggregate count reported below.
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

    # Aggregate observability: per-station drops are logged above; this rolls
    # them into a single count so a caller can see at a glance how many of the
    # candidate stations were lost to fetch failures.
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

    # A requested variable reported by no station in the selection produces no
    # data column and so is silently absent from the output. Name each such
    # variable on stderr; the run still succeeds with the variables that do have
    # data (this is not a failure, just a visible omission).
    missing_vars = [v for v in variables if v not in ds.data_vars]
    if missing_vars:
        print(
            f"Warning: requested variable(s) {sorted(missing_vars)} were reported by no "
            f"station in the selection for {start_iso}..{end_iso}; they are omitted from "
            "the output. The store still carries the variables that had data.",
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
    _set_write_encoding(ds)
    return ds

if __name__ == "__main__":
    fetch()
