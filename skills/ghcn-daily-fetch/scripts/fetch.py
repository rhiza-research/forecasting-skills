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
"""Fetch NOAA GHCN-Daily station observations over HTTPS and write a station-schema Rhiza Envelope Zarr."""

import argparse
import io
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"

# Public, credential-free GHCN-Daily on the S3 website endpoint.
_BASE_URL = "https://noaa-ghcn-pds.s3.amazonaws.com"
_STATIONS_URL = f"{_BASE_URL}/ghcnd-stations.txt"
# Per-station CSV (gzip, NO header). Positional columns:
# ID, DATE(YYYYMMDD), ELEMENT, VALUE, M_FLAG, Q_FLAG, S_FLAG, OBS_TIME.
_STATION_CSV_URL = _BASE_URL + "/csv.gz/by_station/{station_id}.csv.gz"
_CSV_COLUMNS = ["ID", "DATE", "ELEMENT", "VALUE", "M_FLAG", "Q_FLAG", "S_FLAG", "OBS_TIME"]
HTTP_TIMEOUT = 60
DEFAULT_WORKERS = 8

# Canonical envelope variable -> (GHCN element, scale, units, standard_name, cell_method).
# GHCN stores PRCP in tenths of mm and TMAX/TMIN/TAVG in tenths of degrees C, so
# the scale brings raw integers to mm/day and degrees C respectively.
VAR_MAP = {
    "precip": ("PRCP", 0.1, "mm/day", "lwe_precipitation_rate", "time: sum"),
    "tmax": ("TMAX", 0.1, "degC", "air_temperature", "time: maximum"),
    "tmin": ("TMIN", 0.1, "degC", "air_temperature", "time: minimum"),
    "tavg": ("TAVG", 0.1, "degC", "air_temperature", "time: mean"),
}
DEFAULT_VARIABLES = ["precip", "tmax", "tmin"]

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


def _parse_bbox(bbox: str) -> tuple:
    try:
        north, west, south, east = (float(x) for x in bbox.split("/"))
    except ValueError:
        print("Error: --bbox must be four decimal degrees N/W/S/E.", file=sys.stderr)
        sys.exit(2)
    return north, west, south, east


def _load_stations(bbox: str | None):
    """Fetch and parse ghcnd-stations.txt, optionally filtered to a bbox.

    Returns a DataFrame indexed by station ID with latitude/longitude/name.
    """
    import pandas as pd
    import requests

    resp = requests.get(_STATIONS_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    # Fixed-width per the GHCN-Daily readme: ID 1-11, LAT 13-20, LON 22-30,
    # NAME 42-71 (1-indexed inclusive -> 0-indexed half-open colspecs below).
    stations = pd.read_fwf(
        io.StringIO(resp.text),
        colspecs=[(0, 11), (12, 20), (21, 30), (41, 71)],
        names=["station_id", "latitude", "longitude", "name"],
    )
    stations["name"] = stations["name"].astype(str).str.strip()
    if bbox is not None:
        north, west, south, east = _parse_bbox(bbox)
        stations = stations[
            (stations["latitude"] >= south)
            & (stations["latitude"] <= north)
            & (stations["longitude"] >= west)
            & (stations["longitude"] <= east)
        ]
    return stations.set_index("station_id")


def _is_transient(exc: Exception) -> bool:
    """Heuristic: does this error look like a retryable transient/rate-limit?"""
    text = str(exc).lower()
    markers = ("429", "500", "502", "503", "504", "timed out", "timeout", "connection")
    return any(m in text for m in markers)


def _fetch_station_csv(station_id: str):
    """GET one station's gzip CSV bytes, retrying once on a transient error.

    Returns the response content (bytes), or None on HTTP 404 (station has no
    by_station file). Raises on a non-transient error or a surviving transient.
    """
    import requests

    url = _STATION_CSV_URL.format(station_id=station_id)
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            if attempt == 0 and _is_transient(exc):
                time.sleep(2.0)
                continue
            raise
    return None


def _station_frame(station_id: str, elements: dict, start_int: int, end_int: int):
    """Return a daily DataFrame (time index, canonical-variable columns) for one
    station within the window, or None when it has no usable rows.

    `elements` maps GHCN element code -> canonical variable name.
    """
    import pandas as pd

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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--start",
        required=True,
        help=(
            "Start date (inclusive). Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). For "
            "GHCN-Daily 'latest' resolves to the current UTC date (no cheap "
            "day-precise discovery); a missing trailing tail near today is normal."
        ),
    )
    p.add_argument(
        "--end",
        required=True,
        help="End date (inclusive). Same date grammar as --start.",
    )
    p.add_argument(
        "--bbox",
        help="Spatial subset N/W/S/E decimal degrees, filtering stations. Omit to select ALL stations (very large).",
    )
    p.add_argument(
        "--variable",
        "-v",
        action="append",
        choices=sorted(VAR_MAP.keys()),
        help=f"Restrict to this variable; repeat once per variable. Omit for default {DEFAULT_VARIABLES}.",
    )
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            f"Max concurrent per-station download threads (default {DEFAULT_WORKERS}). "
            "Lower this if the server returns throttling errors."
        ),
    )
    args = p.parse_args()

    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        sys.exit(2)

    variables = args.variable or list(DEFAULT_VARIABLES)
    # element code -> canonical variable name, for the requested variables.
    elements = {VAR_MAP[v][0]: v for v in variables}

    import pandas as pd
    import xarray as xr

    # `latest` for GHCN-Daily resolves to today (UTC): there is no cheap
    # day-precise discovery endpoint, and the publication lag means the trailing
    # day or two may simply be absent, which is handled as a normal partial tail.
    def _latest() -> date:
        return datetime.now(UTC).date()

    start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)
    start_int = int(start_date.strftime("%Y%m%d"))
    end_int = int(end_date.strftime("%Y%m%d"))

    entry = {
        "skill": "ghcn-daily-fetch",
        "version": _RHIZA_SKILL_VERSION,
        # --workers is a concurrency knob, excluded from the cache key. Variables
        # are sorted so flag order does not change the key. start/end record the
        # resolved concrete window, never the relative token.
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

    if args.bbox is None:
        print(
            "Note: no --bbox given; selecting ALL GHCN-Daily stations (very large). "
            "Pass --bbox N/W/S/E to restrict.",
            file=sys.stderr,
        )
    stations = _load_stations(args.bbox)
    if stations.empty:
        print("Error: no stations in the requested bbox.", file=sys.stderr)
        sys.exit(1)
    print(
        f"Fetching {len(stations)} candidate stations for {start_iso}..{end_iso}", file=sys.stderr
    )

    frames = []
    meta_rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_station_frame, sid, elements, start_int, end_int): sid
            for sid in stations.index
        }
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                daily = fut.result()
            except Exception as exc:  # noqa: BLE001 -- isolate per-station failures
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

    if not frames:
        print(
            f"Error: no GHCN-Daily observations for {sorted(variables)} in "
            f"{start_iso}..{end_iso} within the requested area.",
            file=sys.stderr,
        )
        sys.exit(1)

    df = pd.concat(frames).reset_index()
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
    ds["station_id"].attrs.update(cf_role="timeseries_id", long_name="GHCN station identifier")
    ds["name"].attrs.update(long_name="station name")
    for canonical in ds.data_vars:
        _element, _scale, units, std_name, cell_method = VAR_MAP[canonical]
        ds[canonical].attrs.update(standard_name=std_name, units=units, cell_methods=cell_method)
    ds.attrs.update(
        rhiza_source="ghcn-daily",
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
