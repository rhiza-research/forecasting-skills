# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cftime",
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
"""Fetch TAHMO station observations and write a station-schema weather-skills envelope Zarr.

Uses the TAHMO Python SDK directly. Credentials come from the environment:
TAHMO_API_USERNAME and TAHMO_API_PASSWORD.
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
_SKILL_VERSION = "0.1.10"

# How far back from today the `latest` resolver requests observations to find
# the newest available TAHMO observation date. Station reporting can lag a few
# days; 30 days of margin covers normal lag plus short gaps. No observation in
# that window exits non-zero.
_LATEST_LOOKBACK_DAYS = 30

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

# Strict absolute-date shape. date.fromisoformat on 3.12 also accepts compact
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

    `now` is the current UTC date. `latest_fn` is a zero-arg callable that
    discovers the newest available date for this source; it is invoked at most
    once per process (the caller memoizes) and only when a token references
    `latest`.
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
    Exits 2 (pre-network) on a malformed token or a reversed range. `latest_fn`
    is called only if a token references `latest`, and at most once.
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
    "precip": ("lwe_precipitation_rate", "mm day-1"),
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
            # compatibility read for the rhiza_ attr prefix; scheduled for removal
            raw = ds.attrs.get("weather_skills_history") or ds.attrs.get("rhiza_history")
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
        # A present-but-non-array value is malformed under the weather_skills_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed weather_skills_history on {zarr_path}; "
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
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=f"skill version: {_SKILL_VERSION}",
    )
    p.add_argument(
        "--country",
        action="append",
        required=True,
        help="Country name (pass once per country)",
    )
    p.add_argument(
        "--start",
        required=True,
        help=(
            "Start date (inclusive). Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days)."
        ),
    )
    p.add_argument(
        "--end",
        required=True,
        help=(
            "End date (inclusive). Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days)."
        ),
    )
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

    import pandas as pd
    import xarray as xr

    # Lazily build (and memoize) the authenticated TAHMO api, the normalized
    # station table, and variable metadata. The setup is shared between `latest`
    # discovery (which must run before the cache key is built) and the main
    # fetch. It runs only when first needed: an all-absolute or now-only window
    # that cache-hits performs no auth, so absolute behavior is unchanged.
    _setup = {}

    def _ensure_setup():
        if "api" not in _setup:
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
            countries_local = list(args.country)
            unknown = [c for c in countries_local if c not in COUNTRY_CODE]
            if unknown:
                print(
                    f"Error: unknown countries {unknown}. Known: {sorted(COUNTRY_CODE)}",
                    file=sys.stderr,
                )
                sys.exit(2)
            api_local = apiWrapper()
            api_local.setCredentials(username, password)
            stations_raw = api_local.getStations()
            stations_local = pd.json_normalize(list(stations_raw.values()), sep="_")
            _setup["api"] = api_local
            _setup["stations"] = stations_local
            # Discover units / description from TAHMO so we don't hard-code them.
            _setup["var_meta"] = api_local.getVariables()
        return _setup["api"], _setup["stations"], _setup["var_meta"]

    def _discover_latest() -> date:
        """`latest` resolver for TAHMO: newest observation date over a bounded
        lookback ending today, across the requested countries' stations.

        Requests controlled raw data over the last ``_LATEST_LOOKBACK_DAYS`` days
        for each candidate TA-coded station and takes the max observation date
        across ALL stations (not the first station that returns data): station
        reporting cadence varies, so the first responder is not necessarily the
        freshest.

        Observation times are filtered to ``<= today`` (UTC) BEFORE taking the
        max, so `latest` is always a real observation day on or before today
        rather than a value clamped to today with no same-day data behind it (a
        station with a future-skewed clock therefore cannot push `latest` past a
        day it actually reported).

        Error vs no-data taxonomy (mirrors chirps/ecmwf): a per-station fetch
        that RAISES (auth/transport/HTTP) is distinguished from one that responds
        with no observations. If EVERY candidate station raised — i.e. not one
        station responded — that is an auth/transport problem, surfaced as a real
        error (exit non-zero), never misreported as "no data". Only when at least
        one station responded but none carried an in-window observation is the
        "no observations" case reported (exit 2).
        """
        api_l, stations_l, _ = _ensure_setup()
        today = datetime.now(UTC).date()
        lookback_start = (today - timedelta(days=_LATEST_LOOKBACK_DAYS)).isoformat()
        today_iso = today.isoformat()
        max_obs_date = None
        candidate_count = 0
        responded_count = 0  # stations that returned WITHOUT raising (data or empty)
        last_error = None
        for country in list(args.country):
            code = COUNTRY_CODE[country]
            sub = stations_l[stations_l["location_countrycode"] == code]
            sub = sub[sub["code"].str.startswith("TA")]
            for _, row in sub.iterrows():
                sid = row["code"]
                candidate_count += 1
                try:
                    raw = _fetch_raw(api_l, sid, lookback_start, today_iso)
                except Exception as exc:  # noqa: BLE001 -- classified below
                    last_error = f"{sid}: {exc}"
                    continue
                responded_count += 1
                if raw is None or len(raw) == 0:
                    continue
                times = pd.to_datetime(raw["time"], format="mixed", utc=True)
                obs_dates = times.dt.date
                # Keep only observations on or before today (UTC) before taking
                # the max, so a future-skewed station clock cannot inflate latest.
                obs_dates = [d for d in obs_dates if d <= today]
                if not obs_dates:
                    continue
                station_max = max(obs_dates)
                if max_obs_date is None or station_max > max_obs_date:
                    max_obs_date = station_max
        if max_obs_date is not None:
            return max_obs_date
        if candidate_count > 0 and responded_count == 0:
            # No station responded — every candidate raised. This is an
            # auth/transport problem, not an empty dataset; surface it.
            print(
                f"Error: every candidate TAHMO station ({candidate_count}) failed to "
                f"respond while resolving 'latest' (last error: {last_error}); this is "
                "an auth/transport problem, not a not-yet-reported window.",
                file=sys.stderr,
            )
            sys.exit(1)
        # At least one station responded (or there were no candidates), but none
        # carried an in-window observation on or before today: a genuine no-data
        # case.
        print(
            f"Error: no TAHMO observations in the last {_LATEST_LOOKBACK_DAYS} days "
            f"({lookback_start}..{today_iso}) for the requested countries; "
            "cannot resolve 'latest'.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Resolve --start/--end to concrete inclusive dates. Malformed tokens and
    # post-resolution reversed ranges exit 2 before any fetch. A `latest` token
    # triggers discovery (authenticated lookback query, run at most once); an
    # all-absolute or now-only window performs no discovery. Absolute YYYY-MM-DD
    # endpoints normalize through date.fromisoformat, so the resolved isoformat
    # is byte-identical to the raw input — absolute behavior is unchanged.
    start_date, end_date, log_line = _resolve_window(args.start, args.end, _discover_latest)
    start = start_date.isoformat()
    end = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    entry = {
        "skill": "tahmo-fetch",
        "version": _SKILL_VERSION,
        # Sort --country so that `--country Ghana --country Kenya` and
        # `--country Kenya --country Ghana` produce identical cache keys.
        # --workers is a concurrency knob, not a data parameter, so it is
        # excluded from the cache key: the same request at any worker count
        # produces the same data. start/end record the RESOLVED concrete window,
        # never the relative token.
        "args": {k: v for k, v in vars(args).items() if k not in {"input", "output", "workers"}}
        | {"country": sorted(args.country), "start": start, "end": end},
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    api, stations, var_meta = _ensure_setup()
    countries = list(args.country)

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
        daily = _station_frame(api, sid, start, end)
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
        attrs = {"coordinates": "latitude longitude"}
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
        weather_skills_source="tahmo",
        weather_skills_history=json.dumps([entry], sort_keys=True),
        featureType="timeSeries",
        Conventions="CF-1.13",
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
