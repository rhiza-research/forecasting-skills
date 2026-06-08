# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "requests",
#   "cf_xarray",
#   "cf_units",
# ]
# ///
"""Fetch OpenAQ v3 air-quality station observations and write a station-schema Rhiza Envelope Zarr.

Uses the OpenAQ v3 REST API. The API key comes from the environment: OPENAQ_API_KEY.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# Imported at module top (not deferred into functions) so a missing dependency
# fails fast at startup — before any per-sensor network work — rather than only
# at the final write after every download has already run.
import cf_units
import cf_xarray  # noqa: F401 -- registers the `.cf` accessor
import numpy as np
import pandas as pd
import requests
import xarray as xr

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"

_API_BASE = "https://api.openaq.org/v3"
HTTP_TIMEOUT = 60
DEFAULT_WORKERS = 8
_PAGE_LIMIT = 1000

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

# --- Relative-date value grammar (duplicated per CONVENTIONS.md; no shared module) ---
#
# A --start/--end value is one of:
#   YYYY-MM-DD                  absolute date
#   now | today                 current UTC date
#   latest                      newest date with available data (per-source)
#   now-<int>{d|w}              now minus N days   (w = 7 days)
#   latest-<int>{d|w}           latest minus N days
# Anything else (months/years, future "+", junk) is rejected pre-network.
_REL_OFFSET_RE = re.compile(r"^(?P<base>now|latest)-(?P<n>\d+)(?P<unit>[dw])$")

# Strict absolute-date shape. date.fromisoformat on 3.11+ also accepts compact
# (20260501) and ISO-week (2026-W18-1) forms; the documented grammar is exactly
# YYYY-MM-DD, so we gate on this regex first and reject the looser forms.
_ABS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Upper bound on a relative offset's resolved day count. 36525 days (~100 years)
# is far beyond any real window yet small enough that the date arithmetic cannot
# raise OverflowError. Rejecting above this cap keeps the failure pre-network.
_MAX_OFFSET_DAYS = 36525


def _parse_token(value: str) -> tuple:
    """Parse a --start/--end value into a structured token.

    Returns one of:
      ("abs", date)                              absolute YYYY-MM-DD
      ("base", "now")                            current UTC date
      ("base", "latest")                         newest available date (resolved later)
      ("offset", "now", n_days, unit_phrase)     now minus n_days
      ("offset", "latest", n_days, unit_phrase)  latest minus n_days

    `unit_phrase` describes the offset in its requested units for the log line
    (e.g. "3-week", "7-day"). Raises ValueError for anything else (months/years,
    future "+", malformed), so the failure happens before any network call.
    "today" is accepted as an alias for "now".
    """
    if value in ("now", "today"):
        return ("base", "now")
    if value == "latest":
        return ("base", "latest")
    m = _REL_OFFSET_RE.match(value)
    if m is not None:
        n = int(m.group("n"))
        if n < 1:
            raise ValueError(
                f"invalid date value {value!r}: offset must be >= 1 (e.g. now-1d, latest-3w)"
            )
        unit = m.group("unit")
        n_days = n * 7 if unit == "w" else n
        if n_days > _MAX_OFFSET_DAYS:
            raise ValueError(
                f"invalid date value {value!r}: offset resolves to {n_days} days, "
                f"above the maximum of {_MAX_OFFSET_DAYS} days (~100 years)"
            )
        unit_phrase = f"{n}-{'week' if unit == 'w' else 'day'}"
        return ("offset", m.group("base"), n_days, unit_phrase)
    if _ABS_DATE_RE.match(value):
        try:
            return ("abs", date.fromisoformat(value))
        except ValueError:
            pass
    raise ValueError(
        f"invalid date value {value!r}: expected an absolute date YYYY-MM-DD, "
        "'now'/'today', 'latest', or an offset 'now-<int>{d|w}' / "
        "'latest-<int>{d|w}'"
    )


def _token_base_date(tok: tuple, now: date, latest_fn) -> date:
    """Resolve a parsed token's base date.

    `now` is the current UTC date. `latest_fn` is a zero-arg callable returning
    the newest available date; invoked only when a token references `latest`.
    """
    kind = tok[0]
    if kind == "abs":
        return tok[1]
    base = tok[1]
    base_date = now if base == "now" else latest_fn()
    if kind == "base":
        return base_date
    return base_date - timedelta(days=tok[2])


def _resolve_window(start_value: str, end_value: str, latest_fn) -> tuple:
    """Resolve --start/--end values to concrete inclusive (start, end) dates.

    Applies the value grammar and the boundary rules:
      - absolute endpoints and ordinary relative ranges are inclusive both ends;
      - the DURATION IDIOM (start is `B-<int>{d|w}` and end is exactly the same
        base token `B`, both `now` or both `latest`) yields an N-day window
        inclusive of the base, with the far edge shifted in by one.

    Returns (start_date, end_date, log_line) where log_line is a stderr message
    to print before fetching when any relative token is present, else None.
    Exits 2 (pre-network) on a malformed token or a reversed range.
    """
    try:
        start_tok = _parse_token(start_value)
        end_tok = _parse_token(end_value)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    relative_used = start_tok[0] != "abs" or end_tok[0] != "abs"
    now = datetime.now(UTC).date()

    # Duration idiom: start is an offset off base B, end is exactly base B.
    duration = start_tok[0] == "offset" and end_tok[0] == "base" and start_tok[1] == end_tok[1]

    start_date = _token_base_date(start_tok, now, latest_fn)
    end_date = _token_base_date(end_tok, now, latest_fn)

    if duration:
        # Window is exactly N days, inclusive of the base end, far edge shifted
        # in by one: start moves forward one day so [end-(N-1), end] spans N days.
        n_days = start_tok[2]
        start_date = end_date - timedelta(days=n_days - 1)
        reason = f"duration mode: {start_tok[3]} window inclusive of {start_tok[1]}"
    else:
        reason = "inclusive both ends"

    if start_date > end_date:
        print(
            f"Error: resolved --start {start_date.isoformat()} is after resolved "
            f"--end {end_date.isoformat()}; the range is reversed.",
            file=sys.stderr,
        )
        sys.exit(2)

    log_line = None
    if relative_used:
        span = (end_date - start_date).days + 1
        log_line = (
            f'resolved "{start_value}".."{end_value}" -> '
            f"{start_date.isoformat()}..{end_date.isoformat()} "
            f"({span} days; {reason})"
        )
    return start_date, end_date, log_line


def _load_history(zarr_path: Path) -> list:
    try:
        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            raw = ds.attrs.get("rhiza_history")
    except (FileNotFoundError, KeyError, ValueError):
        # A not-yet-existing or unreadable output during a cache check is a miss.
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


def _require_key() -> str:
    key = os.environ.get("OPENAQ_API_KEY")
    if not key:
        print(
            "Error: OPENAQ_API_KEY must be set (free key from https://explore.openaq.org/register).",
            file=sys.stderr,
        )
        sys.exit(2)
    return key


def _is_transient(exc: Exception) -> bool:
    """Heuristic: does this error look like a retryable transient/rate-limit?"""
    text = str(exc).lower()
    markers = ("429", "500", "502", "503", "504", "timed out", "timeout", "connection")
    return any(m in text for m in markers)


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

    Pages with `limit`/`page` until a short page is returned. A single transient
    error per page is retried once after a short backoff.
    """
    page = 1
    while True:
        page_params = dict(params, limit=_PAGE_LIMIT, page=page)
        for attempt in range(2):
            try:
                resp = session.get(url, params=page_params, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == 0 and _is_transient(exc):
                    time.sleep(2.0)
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
        print(
            f"Error: {_classify_api_error(exc, 'listing locations in the bbox')}", file=sys.stderr
        )
        sys.exit(1)
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


def _validate_udunits(units: str, variable: str) -> None:
    """Raise SystemExit with an actionable message if `units` is not udunits-valid.

    A CF data variable's `units` must parse under udunits; emitting an
    unparseable string while claiming CF compliance is a false claim. cf_units
    wraps the same udunits-2 library cf-checker uses, so this is a real check,
    not a regex. OpenAQ units are forwarded verbatim and never converted, so a
    genuine parse failure means the provider reported a unit this skill cannot
    pass through honestly — it halts rather than fabricate a normalization.

    A missing/empty units value is rejected up front: `cf_units.Unit(None)` and
    `cf_units.Unit("")` return an "unknown" unit instead of raising, so without
    this guard a `units=None`/empty variable would slip through under a CF-1.13
    claim. Such sensors are dropped earlier (in reconciliation), so reaching here
    with a blank unit is a stamping bug — fail loudly rather than write it.
    """
    if _is_blank_units(units):
        print(
            f"Error: parameter {variable!r} has a missing/empty units value; a CF "
            "data variable must carry udunits-valid units, and writing `units=None` "
            "under a CF-1.13 claim is invalid.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        cf_units.Unit(units)
    except ValueError as exc:
        print(
            f"Error: OpenAQ unit {units!r} for parameter {variable!r} is not "
            f"udunits-valid ({exc}); refusing to write a non-CF store under a "
            "CF-1.13 claim and refusing to fabricate a conversion.",
            file=sys.stderr,
        )
        sys.exit(1)


def _stamp_cf_dsg(ds, units_by_param: dict) -> None:
    """Stamp full CF-1.13 timeSeries DSG attributes onto a station dataset.

    Sets the auxiliary-coordinate attrs (lat/lon/time + the timeseries_id role),
    and on every data variable the load-bearing `coordinates` attr, the verbatim
    OpenAQ `units` (validated here), `long_name`, `cell_methods`, and a CF
    `standard_name` only where one cleanly applies to the reported unit family.
    The ragged station-time cells that are NaN where a sensor did not report on a
    given day are handled via the `_FillValue` write encoding set by the caller.
    The global attrs (Conventions/featureType/title/source/...) are set by the
    caller.
    """
    ds["latitude"].attrs.update(
        standard_name="latitude", long_name="station latitude", units="degrees_north", axis="Y"
    )
    ds["longitude"].attrs.update(
        standard_name="longitude", long_name="station longitude", units="degrees_east", axis="X"
    )
    ds["time"].attrs.update(standard_name="time", long_name="time", axis="T")
    ds["station_id"].attrs.update(cf_role="timeseries_id", long_name="OpenAQ location identifier")
    if "name" in ds.coords or "name" in ds.variables:
        ds["name"].attrs.update(long_name="location name")

    for param in ds.data_vars:
        units = units_by_param[param]
        _validate_udunits(units, param)
        attrs = {
            # `coordinates` is the load-bearing DSG attr: it ties each data
            # variable to its auxiliary lat/lon coords and the time coord.
            "coordinates": "latitude longitude time",
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
        ds[param].attrs.update(attrs)


def _verify_cf_dsg(ds) -> None:
    """Confirm cf-xarray resolves the timeSeries geometry before writing.

    cf-xarray identifies the DSG off `cf_role="timeseries_id"` and resolves the
    spatiotemporal axes off the coord attrs. If any of those do not resolve, the
    stamping is wrong and the store would falsely claim CF-1.13 compliance, so
    fail loudly rather than write it.
    """
    problems = []
    cf_roles = ds.cf.cf_roles
    # Membership, not exact list-equality: cf-xarray returns the resolved role as
    # a list, and a correctly-stamped store must not be rejected over that list's
    # shape or order — only over station_id being absent from it.
    if "station_id" not in cf_roles.get("timeseries_id", []):
        problems.append(f"cf_role timeseries_id did not resolve to station_id (got {cf_roles})")
    for name in ("latitude", "longitude", "time"):
        try:
            ds.cf[name]
        except KeyError:
            problems.append(f"cf-xarray could not resolve the {name} coordinate")
    if problems:
        print(
            "Error: CF-1.13 DSG verification failed before write:\n  - " + "\n  - ".join(problems),
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--start",
        required=True,
        help=(
            "Start date (inclusive). Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "'latest' resolves to the current UTC date for this source."
        ),
    )
    p.add_argument(
        "--end", required=True, help="End date (inclusive). Same date grammar as --start."
    )
    p.add_argument(
        "--bbox",
        required=True,
        help="Spatial subset N/W/S/E decimal degrees (required — selects stations).",
    )
    p.add_argument(
        "--variable",
        "-v",
        action="append",
        choices=SUPPORTED_PARAMETERS,
        help=f"Restrict to this pollutant; repeat once per variable. Omit for all {SUPPORTED_PARAMETERS}.",
    )
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            f"Max concurrent per-sensor fetch threads (default {DEFAULT_WORKERS}). "
            "Lower this if OpenAQ returns 429/throttling errors."
        ),
    )
    args = p.parse_args()

    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        sys.exit(2)

    try:
        north, west, south, east = (float(x) for x in args.bbox.split("/"))
    except ValueError:
        print("Error: --bbox must be four decimal degrees N/W/S/E.", file=sys.stderr)
        sys.exit(2)

    variables = args.variable or list(SUPPORTED_PARAMETERS)

    # `latest` resolves to today (UTC): OpenAQ has no cheap global day-precise
    # discovery, and a thin trailing tail of not-yet-reported days is normal.
    def _latest() -> date:
        return datetime.now(UTC).date()

    start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    entry = {
        "skill": "openaq-fetch",
        "version": _RHIZA_SKILL_VERSION,
        # --workers excluded (concurrency, not data); variables sorted so flag
        # order does not change the key; start/end are the resolved dates.
        "args": {
            "bbox": args.bbox,
            "variable": sorted(variables),
            "start": start_iso,
            "end": end_iso,
        },
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    key = _require_key()
    session = requests.Session()
    session.headers.update({"X-API-Key": key})

    wanted = set(variables)
    sensors = _find_sensors(session, (north, west, south, east), wanted)
    if not sensors:
        print(
            f"Error: no OpenAQ sensors for {sorted(variables)} in bbox {args.bbox}.",
            file=sys.stderr,
        )
        sys.exit(1)

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
        print(
            f"Error: no OpenAQ sensors for {sorted(variables)} in bbox {args.bbox} "
            "survived unit reconciliation.",
            file=sys.stderr,
        )
        sys.exit(1)

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
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
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
                    print(
                        f"Error: {_classify_api_error(exc, 'fetching daily sensor values')}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
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
        print(
            f"Error: no OpenAQ observations for {sorted(variables)} in {start_iso}..{end_iso}.",
            file=sys.stderr,
        )
        sys.exit(1)

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

    _stamp_cf_dsg(ds, units_present)
    ds.attrs.update(
        Conventions="CF-1.13",
        featureType="timeSeries",
        title="OpenAQ air-quality station observations",
        source="OpenAQ v3 API (https://api.openaq.org/v3)",
        institution="OpenAQ",
        references="https://docs.openaq.org/",
        history=f"{datetime.now(UTC).isoformat()} openaq-fetch {start_iso}..{end_iso}",
        rhiza_source="openaq",
        rhiza_history=json.dumps([entry], sort_keys=True),
    )

    # Clear per-variable encoding (not part of the envelope contract), then carry
    # udunits-valid reference-time units + a calendar in the time encoding so the
    # on-disk time axis is fully CF. Encoding is reset BEFORE setting time units
    # so the clear cannot drop them.
    for v in ds.variables:
        ds[v].encoding = {}
    ds["time"].encoding["units"] = _TIME_UNITS
    ds["time"].encoding["calendar"] = _TIME_CALENDAR
    # `_FillValue` is an encoding key, not a CF attribute: set it here, after the
    # per-variable encoding clear, so the on-disk store represents missing
    # station-time cells with a real fill (and reopens with the NaNs intact).
    for param in ds.data_vars:
        ds[param].encoding["_FillValue"] = np.float64(np.nan)

    # Write-side decode check: confirm cf-xarray resolves the DSG geometry
    # (timeseries_id) and the lat/lon/time axes BEFORE writing. A failure here
    # means the stamping is wrong, so fail loudly rather than emit a store that
    # falsely claims CF compliance.
    _verify_cf_dsg(ds)

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({dict(ds.sizes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
