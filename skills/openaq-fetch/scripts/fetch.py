# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
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
"""Fetch OpenAQ v3 air-quality station observations and write a point_obs weather-skills standard dataset Zarr.

Uses the OpenAQ v3 REST API. API key: OPENAQ_API_KEY.
"""

import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import cf_xarray  # noqa: F401  (fail-fast probe)
import cf_xarray.units  # noqa: F401  (fail-fast probe; configures pint CF registry)
import numpy as np
import pandas as pd
import requests
import xarray as xr
from pint import application_registry as ureg
from pathlib import Path

from weather_skills_core import DataError, weather_skill
from weather_skills_core.cf import stamp_cf_dsg, udunits_error, verify_cf_dsg
from weather_skills_core.standard_utils import apply_write_encoding, is_transient, require_env
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_API_BASE = "https://api.openaq.org/v3"
HTTP_TIMEOUT = 60
DEFAULT_WORKERS = 8
_PAGE_LIMIT = 1000

# Client-side rate limit under OpenAQ's 60/min and 2,000/hour caps (~32 req/min).
_REQUEST_SPACING_S = 1.85
_BACKOFF_429_S = 60.0
_RETRY_AFTER_MAX_S = 300.0
_rate_lock = threading.Lock()
_next_request_start = 0.0

_TIME_UNITS = "days since 1970-01-01"
_TIME_CALENDAR = "proleptic_gregorian"

SUPPORTED_PARAMETERS = ["pm25", "pm10", "no2", "o3", "so2", "co"]

_LONG_NAME = {
    "pm25": "daily mean PM2.5 mass concentration",
    "pm10": "daily mean PM10 mass concentration",
    "no2": "daily mean nitrogen dioxide",
    "o3": "daily mean ozone",
    "so2": "daily mean sulfur dioxide",
    "co": "daily mean carbon monoxide",
}

_STD_NAME_MASS = {
    "pm25": "mass_concentration_of_pm2p5_ambient_aerosol_particles_in_air",
    "pm10": "mass_concentration_of_pm10_ambient_aerosol_particles_in_air",
    "no2": "mass_concentration_of_nitrogen_dioxide_in_air",
    "o3": "mass_concentration_of_ozone_in_air",
    "so2": "mass_concentration_of_sulfur_dioxide_in_air",
    "co": "mass_concentration_of_carbon_monoxide_in_air",
}
_STD_NAME_MOLE = {
    "no2": "mole_fraction_of_nitrogen_dioxide_in_air",
    "o3": "mole_fraction_of_ozone_in_air",
    "so2": "mole_fraction_of_sulfur_dioxide_in_air",
    "co": "mole_fraction_of_carbon_monoxide_in_air",
}
_MASS_CONCENTRATION_REF = ureg.Unit("kg m-3")


def _rate_limit_wait() -> None:
    """Reserve the next free request-start slot, then sleep outside the lock."""
    global _next_request_start
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _next_request_start)
        _next_request_start = slot + _REQUEST_SPACING_S
    delay = slot - now
    if delay > 0:
        time.sleep(delay)


def _retry_backoff(exc: Exception) -> float:
    """Sleep before retrying a transient error; clamp 429 Retry-After to 60–300 s."""
    resp = getattr(exc, "response", None)
    if resp is None or getattr(resp, "status_code", None) != 429:
        return 2.0
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = _BACKOFF_429_S
    else:
        if not math.isfinite(parsed) or parsed < 0:
            parsed = _BACKOFF_429_S
    return min(max(parsed, _BACKOFF_429_S), _RETRY_AFTER_MAX_S)


def _classify_api_error(exc: Exception, context: str) -> str:
    status = None
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
    if status in (401, 403):
        return (
            f"OpenAQ rejected the request while {context} (HTTP {status}): the "
            "OPENAQ_API_KEY is missing, invalid, or lacks access. Check the key "
            "registered at https://explore.openaq.org/register."
        )
    if status is not None:
        return f"OpenAQ request failed while {context} (HTTP {status})."
    return f"OpenAQ request failed while {context} ({exc})."


def _auth_status(exc: Exception):
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    status = getattr(resp, "status_code", None)
    return status if status in (401, 403) else None


def _get_pages(session, url: str, params: dict):
    """Yield results across paginated OpenAQ listing; one transient retry per page."""
    page = 1
    while True:
        page_params = dict(params, limit=_PAGE_LIMIT, page=page)
        for attempt in range(2):
            try:
                _rate_limit_wait()
                resp = session.get(url, params=page_params, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == 0 and is_transient(exc):
                    time.sleep(_retry_backoff(exc))
                    continue
                raise
        results = resp.json().get("results", [])
        yield from results
        if len(results) < _PAGE_LIMIT:
            return
        page += 1


def _find_sensors(session, bbox: tuple, wanted: set) -> list:
    north, west, south, east = bbox
    bbox_param = f"{west},{south},{east},{north}"
    sensors = []
    try:
        for loc in _get_pages(session, f"{_API_BASE}/locations", {"bbox": bbox_param}):
            coords = loc.get("coordinates") or {}
            lat, lon = coords.get("latitude"), coords.get("longitude")
            if lat is None or lon is None:
                continue
            for s in loc.get("sensors", []):
                param = (s.get("parameter") or {}).get("name")
                if param not in wanted:
                    continue
                sensors.append(
                    {
                        "sensor_id": s["id"],
                        "parameter": param,
                        "units": (s.get("parameter") or {}).get("units"),
                        "station_id": str(loc["id"]),
                        "name": loc.get("name") or str(loc["id"]),
                        "latitude": float(lat),
                        "longitude": float(lon),
                    }
                )
    except requests.RequestException as exc:
        raise DataError(_classify_api_error(exc, "listing locations in the bbox")) from None
    return sensors


def _sensor_daily(session, desc: dict, start_iso: str, end_iso: str):
    url = f"{_API_BASE}/sensors/{desc['sensor_id']}/days"
    params = {"date_from": start_iso, "date_to": end_iso}
    rows = []
    for r in _get_pages(session, url, params):
        value = r.get("value")
        period = (r.get("period") or {}).get("datetimeFrom") or {}
        stamp = period.get("utc")
        if value is None or not stamp:
            continue
        rows.append((stamp[:10], float(value)))
    return rows or None


def _is_blank_units(units) -> bool:
    return units is None or (isinstance(units, str) and not units.strip())


def _unit_family(units: str):
    """Classify CF/UDUNITS-valid units as mass / mole / None for CF standard_name."""
    if _is_blank_units(units):
        return None
    u = ureg.Unit(units)
    if u.is_compatible_with(_MASS_CONCENTRATION_REF):
        return "mass"
    if u.dimensionless:
        return "mole"
    return None


def _var_attrs(ds, units_by_param: dict) -> dict:
    attrs_by_var = {}
    for param in ds.data_vars:
        units = units_by_param[param]
        if _is_blank_units(units):
            raise DataError(
                f"parameter {param!r} has a missing/empty units value; a CF "
                "data variable must carry udunits-valid units."
            )
        exc = udunits_error(units)
        if exc is not None:
            raise DataError(
                f"OpenAQ unit {units!r} for parameter {param!r} is not "
                f"udunits-valid ({exc}); refusing to write a non-CF store."
            )
        attrs = {
            "units": units,
            "long_name": _LONG_NAME.get(param, param),
            "cell_methods": "time: mean",
        }
        family = _unit_family(units)
        if family == "mass":
            std_name = _STD_NAME_MASS.get(param)
        elif family == "mole":
            std_name = _STD_NAME_MOLE.get(param)
        else:
            std_name = None
        if std_name:
            attrs["standard_name"] = std_name
        attrs_by_var[param] = attrs
    return attrs_by_var


@weather_skill(
    name="openaq-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox", required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=(
        f"Max concurrent per-sensor fetch threads (default {DEFAULT_WORKERS}). "
        "Threads overlap response waits only; request starts are rate-limited "
        "globally under OpenAQ's published limits (60/minute, 2,000/hour)."
    ),
)
def fetch(start_time, end_time, bbox, workers, variable, **kwargs):
    """Fetch OpenAQ v3 air-quality station observations and write a point_obs weather-skills standard dataset Zarr."""
    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    north, west, south, east = bbox
    bbox_label = f"{north}/{west}/{south}/{east}"
    variables = sorted(variable or list(SUPPORTED_PARAMETERS))

    (key,) = require_env(
        "OPENAQ_API_KEY",
        message=(
            "OPENAQ_API_KEY must be set (free key from https://explore.openaq.org/register)."
        ),
    )
    session = requests.Session()
    session.headers.update({"X-API-Key": key})

    sensors = _find_sensors(session, (north, west, south, east), set(variables))
    if not sensors:
        raise DataError(f"no OpenAQ sensors for {sorted(variables)} in bbox {bbox_label}.")

    # First-seen unit per parameter is canonical; mismatched / blank units are dropped.
    units_by_param = {}
    kept_sensors = []
    dropped = 0
    for s in sensors:
        param = s["parameter"]
        unit = s["units"]
        if _is_blank_units(unit):
            dropped += 1
            print(
                f"sensor {s['sensor_id']} ({param}): DROPPED, missing/empty units; "
                "cannot write a CF data variable without udunits-valid units.",
                file=sys.stderr,
            )
            continue
        canonical = units_by_param.setdefault(param, unit)
        if unit != canonical:
            dropped += 1
            print(
                f"sensor {s['sensor_id']} ({param}): DROPPED, unit {unit!r} differs "
                f"from {canonical!r} already seen for {param}; cannot merge mismatched "
                "units into one column.",
                file=sys.stderr,
            )
            continue
        kept_sensors.append(s)
    sensors = kept_sensors

    if not sensors:
        raise DataError(
            f"no OpenAQ sensors for {sorted(variables)} in bbox {bbox_label} "
            "survived unit reconciliation."
        )

    candidate_count = len(sensors)
    print(f"Fetching {candidate_count} sensors for {start_iso}..{end_iso}", file=sys.stderr)

    def _fetch_one(desc):
        rows = _sensor_daily(session, desc, start_iso, end_iso)
        if not rows:
            return None
        frame = pd.DataFrame(rows, columns=["time", desc["parameter"]])
        frame = frame.groupby("time", as_index=True).mean()
        frame.index = pd.to_datetime(frame.index)
        frame["station_id"] = desc["station_id"]
        return frame, desc

    frames = []
    meta_rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, d): d for d in sensors}
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                status = _auth_status(exc)
                if status is not None:
                    raise DataError(
                        _classify_api_error(exc, "fetching daily sensor values")
                    ) from None
                dropped += 1
                print(
                    f"sensor {d['sensor_id']} ({d['parameter']}): DROPPED ({exc})",
                    file=sys.stderr,
                )
                continue
            if result is None:
                continue
            frame, desc = result
            frames.append(frame)
            meta_rows.append(
                {
                    "station_id": desc["station_id"],
                    "latitude": desc["latitude"],
                    "longitude": desc["longitude"],
                    "name": desc["name"],
                }
            )

    if dropped:
        print(
            f"Dropped {dropped} sensors (failed fetch + one retry, unit mismatch, "
            "or missing units).",
            file=sys.stderr,
        )

    if not frames:
        raise DataError(
            f"no OpenAQ observations for {sorted(variables)} in {start_iso}..{end_iso}."
        )

    df = pd.concat(frames).reset_index().rename(columns={"index": "time"})
    df = df.groupby(["time", "station_id"], as_index=False).mean(numeric_only=True)
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
            f"Warning: requested variable(s) {sorted(missing_vars)} yielded no in-window "
            f"data for {start_iso}..{end_iso}; they are omitted from the output.",
            file=sys.stderr,
        )

    units_present = {param: units_by_param[param] for param in ds.data_vars}
    stamp_cf_dsg(
        ds,
        _var_attrs(ds, units_present),
        station_id_long_name="OpenAQ location identifier",
        name_long_name="location name",
    )
    ds.attrs.update(
        Conventions="CF-1.13",
        featureType="timeSeries",
        title="OpenAQ air-quality station observations",
        source="OpenAQ v3 API (https://api.openaq.org/v3)",
        institution="OpenAQ",
        references="https://docs.openaq.org/",
        history=f"{datetime.now(UTC).isoformat()} openaq-fetch {start_iso}..{end_iso}",
        weather_skills_source="openaq",
    )

    verify_cf_dsg(ds)
    apply_write_encoding(
        ds,
        time_units=_TIME_UNITS,
        time_calendar=_TIME_CALENDAR,
        fills={v: np.float64(np.nan) for v in ds.data_vars},
    )
    return stamp_data_interval(ds)


if __name__ == "__main__":
    fetch()
