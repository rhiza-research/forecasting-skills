# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "requests",
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

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"

_API_BASE = "https://api.openaq.org/v3"
HTTP_TIMEOUT = 60
DEFAULT_WORKERS = 8
_PAGE_LIMIT = 1000

# OpenAQ parameter names exposed as canonical envelope variables. Units are NOT
# hardcoded — they are forwarded from each sensor's parameter.units in the API
# response (µg/m³ for particulates, ppm/ppb for gases, varying by provider).
SUPPORTED_PARAMETERS = ["pm25", "pm10", "no2", "o3", "so2", "co"]

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
        import xarray as xr

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


def _get_pages(session, url: str, params: dict):
    """Yield each result across all pages of an OpenAQ v3 listing endpoint.

    Pages with `limit`/`page` until a short page is returned. A single transient
    error per page is retried once after a short backoff.
    """
    import requests

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
    longitude}. OpenAQ v3 bbox order is min-lon, min-lat, max-lon, max-lat.
    """
    north, west, south, east = bbox
    bbox_param = f"{west},{south},{east},{north}"
    sensors = []
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

    import pandas as pd
    import requests
    import xarray as xr

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
    print(f"Fetching {len(sensors)} sensors for {start_iso}..{end_iso}", file=sys.stderr)

    # units per parameter, taken from the API (first sensor that reports each).
    units = {}
    for s in sensors:
        units.setdefault(s["parameter"], s["units"])

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
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")
    ds["station_id"].attrs.update(cf_role="timeseries_id", long_name="OpenAQ location identifier")
    ds["name"].attrs.update(long_name="location name")
    for param in ds.data_vars:
        unit = units.get(param)
        if unit:
            ds[param].attrs.update(units=unit)
    ds.attrs.update(
        rhiza_source="openaq",
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
