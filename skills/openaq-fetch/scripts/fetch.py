# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
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
"""Fetch OpenAQ v3 air-quality station observations and write a station-schema weather-skills envelope Zarr.

Uses the OpenAQ v3 REST API. The API key comes from the environment: OPENAQ_API_KEY.
"""

import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

# cf_xarray is used only inside weather-skills-core, which imports it lazily at
# write time (verify_cf_dsg) — the final step, after every per-sensor download.
# Importing it eagerly at module top turns that late import into a startup
# fail-fast probe: a missing dependency errors before any network work rather than
# only after every download has run. The F401 noqa marks the probe-only import;
# removing it would drop the fail-fast guarantee. (cf_units is imported for direct
# use in the module-level unit constants below, so it is not a probe.)
import cf_units
import cf_xarray  # noqa: F401  (loaded lazily by core's verify_cf_dsg at write time)
import numpy as np
import pandas as pd
import requests
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.dates import today_utc
from weather_skills_core.envelope import stamp_cf_dsg, udunits_error, verify_cf_dsg
from weather_skills_core.util import is_transient

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.7"

_API_BASE = "https://api.openaq.org/v3"
HTTP_TIMEOUT = 60
DEFAULT_WORKERS = 8
_PAGE_LIMIT = 1000

# --- Client-side rate limiting ---
#
# OpenAQ publishes hard API rate limits: 60 requests/minute and 2,000
# requests/hour (https://docs.openaq.org/using-the-api/rate-limits). Exceeding
# them returns 429s, and sustained overuse can get an API key suspended, so
# compliance is built in rather than optional: EVERY request to the API (the
# locations listing and each per-sensor page) passes through the global
# limiter below, and no flag — including --workers — can raise the request
# rate. Worker threads still overlap on response waits; only request STARTS
# are spaced.
#
# 1.85 s between request starts is a sustained ~32.4 requests/minute and
# ~1,946 requests/hour (3600 / 1.85) — strictly under BOTH published limits
# (60/minute and 2,000/hour) with real margin, staying just under the hourly cap
# rather than sitting exactly on it. The hourly figure is the binding one:
# 3600/1.85 < 2000.
_REQUEST_SPACING_S = 1.85
# Generous fallback backoff before retrying a 429 that carries no usable
# Retry-After header (the limiter should make 429s rare to begin with). Also
# serves as the lower floor on the honored 429 wait (see _retry_backoff).
_BACKOFF_429_S = 60.0
# Upper cap on a honored Retry-After. A server asking us to wait longer than
# this is treated as "wait the cap, then give up on the single retry" rather
# than blocking the run for an unbounded interval.
_RETRY_AFTER_MAX_S = 300.0  # 5 minutes
_rate_lock = threading.Lock()
_next_request_start = 0.0  # monotonic time of the next free request slot


def _rate_limit_wait() -> None:
    """Block until this thread may start the next API request.

    Reserves the next free 1.85 s-spaced start slot under the lock, then sleeps
    outside it, so concurrent threads queue up on the spacing grid without
    serializing their response waits.
    """
    global _next_request_start
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _next_request_start)
        _next_request_start = slot + _REQUEST_SPACING_S
    delay = slot - now
    if delay > 0:
        time.sleep(delay)


# CF time-coordinate encoding. udunits-valid reference-time units plus a
# calendar, carried in the write encoding so the on-disk time axis is fully CF.
_TIME_UNITS = "days since 1970-01-01"
_TIME_CALENDAR = "proleptic_gregorian"

# OpenAQ parameter names exposed as canonical envelope variables. Units are NOT
# hardcoded — they are forwarded verbatim from each sensor's parameter.units in
# the API response (µg/m³ for particulates, ppm/ppb for gases, varying by
# provider) and validated under udunits at write time.
SUPPORTED_PARAMETERS = ["pm25", "pm10", "no2", "o3", "so2", "co"]

# Human-readable label per pollutant, used for the CF `long_name`.
_LONG_NAME = {
    "pm25": "daily mean PM2.5 mass concentration",
    "pm10": "daily mean PM10 mass concentration",
    "no2": "daily mean nitrogen dioxide",
    "o3": "daily mean ozone",
    "so2": "daily mean sulfur dioxide",
    "co": "daily mean carbon monoxide",
}

# CF standard names per pollutant, split by reported-unit family. A pollutant's
# CF standard name is unit-dependent: a mass-concentration reading (µg/m³, mg/m³)
# pairs with a `mass_concentration_of_*_in_air` name, a mole-fraction reading
# (ppm, ppb) with a `mole_fraction_of_*_in_air` name. Each entry below was
# verified at implementation against the CF standard name table (current). Where
# a family has no clean CF entry the key is absent and `standard_name` is omitted
# (units + long_name alone is CF-valid): particulate matter has no mole-fraction
# CF name, so pm25/pm10 carry a standard_name only on the mass-concentration path.
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

# Reference unit for the mass-concentration family. A reported unit that converts
# to this is a mass concentration; a dimensionless unit (ppm/ppb -> "1") is a
# mole fraction. cf_units wraps udunits-2, so this is a real dimensional check.
_MASS_CONCENTRATION_REF = cf_units.Unit("kg m-3")

# --- Source -> output transforms ---
#
# Everything from the raw OpenAQ v3 source is passed through unchanged unless
# listed below. Pass-through is the default; these are the only divergences.
#
# UNITS: pass through verbatim. Each pollutant's `parameter.units` from the API
#   response is written unchanged as the variable's CF `units` attr, validated
#   under udunits (cf_units / udunits-2) at write time. No remap, no unit
#   conversion, no normalization — verbatim forwarding is the default-and-only
#   units behavior. A unit that does not parse halts the run rather than being
#   coerced; a sensor whose unit differs from the first-seen unit for the same
#   pollutant is dropped, never relabeled.
#
# VARIABLE NAMING: no rename. The output data variables ARE the OpenAQ parameter
#   names (pm25/pm10/no2/o3/so2/co); SUPPORTED_PARAMETERS is used directly as the
#   variable names.
#
# standard_name ASSIGNMENT (the notable transform): assigned per pollutant on a
#   basis that depends on the reported unit family. A mass-concentration unit
#   (e.g. µg/m³) yields a `mass_concentration_of_*_in_air` name; a mole-fraction
#   unit (ppm/ppb) yields a `mole_fraction_of_*_in_air` name. The name is omitted
#   where no verified CF standard-name-table entry applies to that pollutant in
#   that unit family (e.g. PM reported in a mole-fraction unit — particulate
#   matter has no mole-fraction CF name); units + long_name alone remain CF-valid
#   there. The mass/mole/none classification comes from a real udunits
#   dimensional check (see `_unit_family`), not the unit string's text.
#
# long_name ASSIGNMENT: a human-readable label per pollutant (the `_LONG_NAME`
#   table), defaulting to the parameter name itself when absent.
#
# cell_methods ASSIGNMENT: every data variable gets `cell_methods="time: mean"`,
#   declaring each daily value as the within-day mean of that sensor's sub-daily
#   measurements.
#
# STATION ENVELOPE ASSEMBLY: the OpenAQ location `id` becomes the `station_id`
#   coordinate (cf_role="timeseries_id"); the location's `coordinates.latitude`
#   and `coordinates.longitude` become the `latitude`/`longitude` station
#   coordinates; the location `name` becomes the `name` coordinate (falling back
#   to the location id when absent). This is the only field rename in the
#   transform set (location id -> station_id); the lat/lon/name values are
#   carried through unchanged.


def _require_key() -> str:
    key = os.environ.get("OPENAQ_API_KEY")
    if not key:
        raise UsageError(
            "OPENAQ_API_KEY must be set (free key from https://explore.openaq.org/register)."
        )
    return key


def _retry_backoff(exc: Exception) -> float:
    """Return how long to sleep before the single retry of a transient error.

    A 429 means OpenAQ is throttling, so the retry must never be tight. The 429
    wait is clamped into a sane band:

        delay = min(max(parsed_or_fallback, _BACKOFF_429_S), _RETRY_AFTER_MAX_S)

    where `parsed_or_fallback` is a finite non-negative numeric Retry-After
    header if present, else `_BACKOFF_429_S`. The floor (`_BACKOFF_429_S`, 60 s)
    keeps a `Retry-After: 0` — or any value below the floor — from producing a
    near-zero, hammering retry; a larger numeric value is honored upward up to
    the cap (`_RETRY_AFTER_MAX_S`, 300 s), beyond which we wait the cap and then
    give up on the single retry. The resulting 429 policy:

        Retry-After: 0      -> 60 s  (floored)
        Retry-After: 5      -> 60 s  (floored)
        Retry-After: 120    -> 120 s (honored)
        Retry-After: 99999  -> 300 s (capped)
        missing / garbage   -> 60 s  (fallback, then floored)
        HTTP-date form      -> 60 s  (not a number -> fallback)
        inf / nan           -> 60 s  (non-finite rejected to fallback)

    A non-finite header (inf/nan) is rejected to the fallback via math.isfinite.
    Non-429 transients keep the short 2 s backoff. The retried request itself
    still passes through the global rate limiter.
    """
    resp = getattr(exc, "response", None)
    if resp is None or getattr(resp, "status_code", None) != 429:
        return 2.0
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = _BACKOFF_429_S
    else:
        # Reject non-finite (inf/nan) and negative values to the fallback;
        # an HTTP-date Retry-After is non-numeric and already fell into except.
        if not math.isfinite(parsed) or parsed < 0:
            parsed = _BACKOFF_429_S
    return min(max(parsed, _BACKOFF_429_S), _RETRY_AFTER_MAX_S)


def _classify_api_error(exc: Exception, context: str) -> str:
    """Map an API exception to a one-line actionable message that never echoes
    the key.

    The X-API-Key header value is never read here — only the HTTP status on the
    response (when present) is inspected. A 401/403 means the key is missing or
    rejected; everything else is reported with its status/text as-is.
    """
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
    """Return the HTTP status if `exc` is an auth failure (401/403), else None.

    Only the status code on the attached response is inspected — never the key.
    Used to tell a per-sensor auth failure (key expired mid-run, or authorized
    for /locations but not /sensors) apart from a routine transient drop.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    status = getattr(resp, "status_code", None)
    return status if status in (401, 403) else None


def _get_pages(session, url: str, params: dict):
    """Yield each result across all pages of an OpenAQ v3 listing endpoint.

    Pages with `limit`/`page` until a short page is returned. Every request
    start passes through the global rate limiter. A single transient error per
    page is retried once after a backoff (2 s for a non-429 transient, or a
    60–300 s clamped wait for a 429 — see `_retry_backoff`).
    """
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
    """Return a list of sensor descriptors inside the bbox for the wanted params.

    Each descriptor: {sensor_id, parameter, units, station_id, name, latitude,
    longitude}. OpenAQ v3 bbox order is min-lon, min-lat, max-lon, max-lat, so
    the N/W/S/E argument is reordered here.

    A failure of the locations query (auth, transport, HTTP) is classified into a
    one-line actionable message that never echoes the key, then exits non-zero —
    rather than surfacing as a raw traceback.
    """
    north, west, south, east = bbox
    bbox_param = f"{west},{south},{east},{north}"
    sensors = []
    try:
        pages = _get_pages(session, f"{_API_BASE}/locations", {"bbox": bbox_param})
        for loc in pages:
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
    """Fetch daily values for one sensor. Returns a list of (date, value) or None."""
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
    """True if a reported unit is missing/empty and so cannot back a CF claim.

    `cf_units.Unit(None)` and `cf_units.Unit("")` do NOT raise — they return an
    "unknown" unit — so a None/empty/whitespace units value would pass the
    udunits guard and be written verbatim, yielding a variable with `units=None`
    (or empty) under a `Conventions="CF-1.13"` global: a false CF claim. Such a
    value is treated as invalid everywhere it could reach an output variable.
    """
    return units is None or (isinstance(units, str) and not units.strip())


def _unit_family(units: str):
    """Classify a udunits-valid unit string into the CF standard-name family.

    Returns "mass" for a mass-concentration unit (µg/m³, mg/m³ — convertible to
    kg m-3), "mole" for a dimensionless mole-fraction unit (ppm, ppb -> "1"), or
    None when neither applies. Used to pick the unit-consistent CF standard_name.
    Assumes `units` already parsed under cf_units (validated by the caller).
    """
    if _is_blank_units(units):
        # Never let cf_units.Unit(None/"") through as a silent "unknown" unit.
        return None
    u = cf_units.Unit(units)
    if u.is_convertible(_MASS_CONCENTRATION_REF):
        return "mass"
    if u.is_dimensionless():
        return "mole"
    return None


def _var_attrs(ds, units_by_param: dict) -> dict:
    """Per-variable CF attr dicts: verbatim OpenAQ units (validated), long_name,
    cell_methods, and a CF standard_name only where one cleanly applies to the
    reported unit family.

    A CF data variable's `units` must parse under udunits; emitting an
    unparseable string while claiming CF compliance is a false claim. cf_units
    wraps the same udunits-2 library cf-checker uses, so this is a real check,
    not a regex. OpenAQ units are forwarded verbatim and never converted, so a
    genuine parse failure means the provider reported a unit this skill cannot
    pass through honestly — it halts rather than fabricate a normalization.

    A missing/empty units value is rejected up front: `cf_units.Unit(None)` and
    `cf_units.Unit("")` return an "unknown" unit instead of raising, so without
    this guard a `units=None`/empty variable would slip through under a CF-1.13
    claim. Such sensors are dropped earlier (in reconciliation), so reaching
    here with a blank unit is a stamping bug — fail loudly rather than write
    it. The ragged station-time cells that are NaN where a sensor did not
    report on a given day are handled via the `_FillValue` write encoding set
    by the write-encoding hook.
    """
    attrs_by_var = {}
    for param in ds.data_vars:
        units = units_by_param[param]
        if _is_blank_units(units):
            raise DataError(
                f"parameter {param!r} has a missing/empty units value; a CF "
                "data variable must carry udunits-valid units, and writing `units=None` "
                "under a CF-1.13 claim is invalid."
            )
        exc = udunits_error(units)
        if exc is not None:
            raise DataError(
                f"OpenAQ unit {units!r} for parameter {param!r} is not "
                f"udunits-valid ({exc}); refusing to write a non-CF store under a "
                "CF-1.13 claim and refusing to fabricate a conversion."
            )
        attrs = {
            "units": units,
            "long_name": _LONG_NAME.get(param, param),
            # Each OpenAQ daily value is the within-day mean of the sub-daily
            # measurements for that sensor.
            "cell_methods": "time: mean",
        }
        # standard_name only where a CF-table entry matches the reported unit
        # family; omitted otherwise (units + long_name alone is CF-valid).
        family = _unit_family(units)
        std_name = None
        if family == "mass":
            std_name = _STD_NAME_MASS.get(param)
        elif family == "mole":
            std_name = _STD_NAME_MOLE.get(param)
        if std_name:
            attrs["standard_name"] = std_name
        attrs_by_var[param] = attrs
    return attrs_by_var


def _normalize_entry_args(raw: dict) -> dict:
    """Canonicalize the recorded cache-key args.

    Variables are sorted (with the full supported list applied when --variable
    is omitted) so flag order does not change the key. --workers (concurrency,
    not data) is already excluded by the decorator.
    """
    raw["variable"] = sorted(raw.get("variable") or SUPPORTED_PARAMETERS)
    return raw


def _set_write_encoding(ds) -> None:
    """Controlled write encodings, applied after the decorator's encoding clear.

    Carry udunits-valid reference-time units + a calendar in the time encoding
    so the on-disk time axis is fully CF. `_FillValue` is an encoding key, not
    a CF attribute: set here so the on-disk store represents missing
    station-time cells with a real fill (and reopens with the NaNs intact).
    """
    ds["time"].encoding["units"] = _TIME_UNITS
    ds["time"].encoding["calendar"] = _TIME_CALENDAR
    for param in ds.data_vars:
        ds[param].encoding["_FillValue"] = np.float64(np.nan)


@weather_skill(
    "openaq-fetch",
    _SKILL_VERSION,
    output_type="station",
    source="openaq",
    start_time={
        "help": (
            "Start date (inclusive). Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "'latest' resolves to the current UTC date for this source."
        )
    },
    end_time={"help": "End date (inclusive). Same date grammar as --start."},
    bbox={
        "mode": "required",
        "help": "Spatial subset N/W/S/E decimal degrees (required — selects stations).",
    },
    workers={
        "default": DEFAULT_WORKERS,
        "help": (
            f"Max concurrent per-sensor fetch threads (default {DEFAULT_WORKERS}). "
            "Threads overlap response waits only; request starts are rate-limited "
            "globally under OpenAQ's published limits (60/minute, 2,000/hour), so "
            "raising this does not raise the request rate."
        ),
    },
    variable={
        "mode": "repeat",
        "choices": SUPPORTED_PARAMETERS,
        "help": (
            "Restrict to this pollutant; repeat once per variable. "
            f"Omit for all {SUPPORTED_PARAMETERS}."
        ),
    },
    latest_resolver=today_utc,
    normalize_args=_normalize_entry_args,
    write_encoding=_set_write_encoding,
    cache_hit_label="fetch",
)
def fetch(start_time, end_time, bbox, workers, variable, context):
    """Fetch OpenAQ v3 air-quality station observations and write a station-schema weather-skills envelope Zarr."""
    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    # Error messages echo the bbox exactly as given on the CLI.
    bbox_raw = context.args.bbox
    north, west, south, east = bbox

    variables = variable or list(SUPPORTED_PARAMETERS)

    key = _require_key()
    session = requests.Session()
    session.headers.update({"X-API-Key": key})

    wanted = set(variables)
    sensors = _find_sensors(session, (north, west, south, east), wanted)
    if not sensors:
        raise DataError(f"no OpenAQ sensors for {sorted(variables)} in bbox {bbox_raw}.")

    # Per-parameter unit reconciliation. OpenAQ reports a unit per sensor; for a
    # given pollutant they are normally identical, but a provider that reports a
    # different unit for the same pollutant cannot be merged into one column
    # without mislabeling it. The first-seen unit per parameter is canonical;
    # any sensor reporting a different unit is dropped with a stderr note and
    # rolled into the aggregate dropped count, never silently relabeled.
    units_by_param = {}
    kept_sensors = []
    dropped = 0
    for s in sensors:
        param = s["parameter"]
        unit = s["units"]
        if _is_blank_units(unit):
            # A sensor with no reported unit cannot back a CF `units` attr; drop
            # it (counted below) rather than write `units=None` under CF-1.13.
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
            f"no OpenAQ sensors for {sorted(variables)} in bbox {bbox_raw} "
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
            except Exception as exc:  # noqa: BLE001 -- isolate per-sensor failures
                # An auth failure (401/403) is NOT a routine per-sensor drop: a
                # key that expired mid-run or lacks /sensors scope would 401 on
                # EVERY sensor and the run would otherwise exit "no observations"
                # with the real cause hidden. Surface it as fatal and stop, so
                # the auth problem is not silently swallowed. Only the response
                # status is inspected — the key is never read or echoed.
                status = _auth_status(exc)
                if status is not None:
                    raise DataError(
                        _classify_api_error(exc, "fetching daily sensor values")
                    ) from None
                # A genuine transient (timeout/5xx/429) that survived the single
                # in-flight retry drops the sensor (not silently lost): logged
                # per-line here and rolled into the aggregate count below.
                dropped += 1
                print(
                    f"sensor {d['sensor_id']} ({d['parameter']}): DROPPED ({exc})", file=sys.stderr
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

    # Aggregate observability: per-sensor drops (failed fetch + retry, and unit
    # mismatches) are logged above; this rolls them into a single count so a
    # caller can see at a glance how many sensors were lost.
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

    # A station may contribute several frames (one per parameter/sensor); group by
    # (time, station_id) and take the mean so duplicate sensors collapse cleanly.
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

    # A requested pollutant whose sensors all returned no in-window data (or were
    # all dropped on fetch failure / unit mismatch / missing units) produces no
    # data column and so is silently absent from the output. Name each such
    # variable on stderr; the run still succeeds with the variables that do have
    # data (this is not a failure, just a visible omission).
    missing_vars = [v for v in variables if v not in ds.data_vars]
    if missing_vars:
        print(
            f"Warning: requested variable(s) {sorted(missing_vars)} yielded no in-window "
            f"data for {start_iso}..{end_iso} (no sensor reported, or all were dropped on "
            "fetch failure / unit mismatch / missing units); they are omitted from the "
            "output. The store still carries the variables that had data.",
            file=sys.stderr,
        )

    # Units carried onto each data variable are the reconciled per-parameter
    # units restricted to the parameters that actually produced a column.
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
    )

    # Write-side decode check: confirm cf-xarray resolves the DSG geometry
    # (timeseries_id) and the lat/lon/time axes BEFORE writing. A failure here
    # means the stamping is wrong, so fail loudly rather than emit a store that
    # falsely claims CF compliance.
    verify_cf_dsg(ds)

    return ds


if __name__ == "__main__":
    fetch()
