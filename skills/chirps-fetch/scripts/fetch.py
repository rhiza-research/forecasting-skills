# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cftime",
#   "requests",
#   "xarray",
#   "zarr",
#   "numpy",
#   "rioxarray",
# ]
# ///
"""Fetch CHIRPS precipitation over HTTPS (final product, prelim fallback) and write a weather-skills envelope Zarr."""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import requests
import rioxarray  # noqa: F401 — registers .rio accessor
import xarray as xr

# Two CHIRPS v3.0 daily `sat` (IMERG-based) products. The FINAL product is the
# validated archive (per-year folders, 1998-to-present); the PRELIM product is
# the rolling recent-only feed published ~2 days after each pentad closes. Each
# requested day prefers final and falls back to prelim (see _download_day_tif),
# so historical dates resolve from final while the recent tail final has not
# finalized yet resolves from prelim.
CHIRPS_FINAL_BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat"
CHIRPS_PRELIM_BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/prelim/sat"
# Earliest year the final `sat` product covers; used only for the friendly
# all-missing diagnostic, not as a hard pre-network gate.
CHIRPS_FINAL_START_YEAR = 1998
CHIRPS_NODATA = -9999.0
HTTP_TIMEOUT = 60

# How far back from today the `latest` backward probe looks for the first
# available CHIRPS prelim day. CHIRPS v3.0 prelim is published 2 days after each
# pentad closes (worst-case lag ~7 days); 30 days of margin comfortably covers
# that plus any short server-side gap. Exhausting the probe without a hit exits
# non-zero.
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


# Default size of the per-day download thread pool. The work is
# network-I/O-bound (one independent HTTPS GET per day), so threads overlap
# request latency without contending on the GIL. CHC's data server publishes
# no concurrency or rate-limit policy, but it can throttle and temporarily
# block IPs under higher concurrency: pools of 8 concurrent downloads have
# repeatedly triggered throttling and temporary IP blocks at this host.
# 2 keeps the request pattern gentle while still overlapping request latency;
# --workers 1 is the fully serial fallback.
DEFAULT_WORKERS = 2


class DayUnavailable(Exception):
    """Raised when a day's TIF cannot be retrieved: not yet published (HTTP 404),
    a transient server (5xx) or network error, or a non-TIFF / truncated / empty
    body. The post-loop classifier then handles tail-vs-mid-gap.

    ``status`` carries the HTTP status code when the failure had one (the 5xx
    raise site); transport-error, truncated, empty, and non-TIFF failures leave
    it None. The post-loop classifier uses it to tell an all-days 5xx refusal
    (server throttling the whole run) apart from genuinely absent data.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.14"


def _load_history(zarr_path: Path) -> list:
    try:
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


def _daterange(start: str, end: str):
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    d = s
    while d <= e:
        yield d
        d += timedelta(days=1)


class _SessionPool:
    """Per-thread requests.Session holder.

    requests.Session is not documented thread-safe — its connection pool and
    cookie jar are not meant to be shared across threads — so each worker
    thread gets its own Session, created lazily on first use and reused for
    every subsequent GET on that thread (one TLS handshake per thread rather
    than one per day). Every created Session is tracked so they can all be
    closed after the pool drains.
    """

    def __init__(self):
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()

    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            self._local.session = s
            with self._lock:
                self._all.append(s)
        return s

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._all)
        for s in sessions:
            s.close()


# Sentinel returned by _get_tif_body when a URL answers 404 (the day is not
# published at that product) — distinct from None and from a validated body, so
# the caller can fall back to the other product rather than failing the day.
_NOT_FOUND = object()


def _get_tif_body(session: requests.Session, url: str):
    """Fetch one CHIRPS day TIF URL and return its validated bytes.

    Returns ``_NOT_FOUND`` on HTTP 404 (the day is not published at this
    product, so the caller may try the other product). Raises ``DayUnavailable``
    for transient failures (transport error, 5xx, truncated/empty/non-TIFF body)
    so the post-loop tail-vs-mid-gap classifier handles them the same way it
    handles a missing day. Re-raises for other 4xx (auth/bad request), which
    indicate a real config problem rather than a not-yet-published day.
    """
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise DayUnavailable(f"transient network error: {exc}") from exc

    if resp.status_code == 404:
        return _NOT_FOUND
    if resp.status_code >= 500:
        raise DayUnavailable(
            f"transient server error: HTTP {resp.status_code} for {url}",
            status=resp.status_code,
        )
    if resp.status_code != 200:
        resp.raise_for_status()

    body = resp.content
    # Guard against a truncated body: when the server reports a length, the
    # received byte count must match before the file is handed to rioxarray.
    # A malformed/non-numeric Content-Length is treated as "no declared
    # length" rather than aborting the whole run with an uncaught ValueError.
    declared = resp.headers.get("Content-Length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except (TypeError, ValueError):
            declared_len = None
        if declared_len is not None and len(body) != declared_len:
            raise DayUnavailable(
                f"truncated body for {url}: got {len(body)} bytes, Content-Length was {declared}"
            )
    if not body:
        raise DayUnavailable(f"empty body for {url}")

    # Reject a 200 response whose body is not actually a TIFF (e.g. an HTML
    # error/landing page): a TIFF/GeoTIFF starts with the little-endian
    # b"II*\x00" or big-endian b"MM\x00*" signature. Rejecting here makes such
    # a response a clean DayUnavailable rather than a confusing downstream
    # rioxarray error after the body has been written and reopened.
    if body[:4] not in (b"II*\x00", b"MM\x00*"):
        raise DayUnavailable(f"non-TIFF body for {url}: leading bytes {body[:4]!r}")

    return body


def _http_refusal_message(exc: requests.HTTPError, workers: int) -> str:
    """Build the abort message for an HTTPError escaping a download worker.

    Always includes status, reason, and URL when a response is attached. The
    hint is status-aware: 403/429 (and a response-less error) read as rate
    limiting / a temporary IP block, where waiting and lowering --workers help;
    any other status reads as a request/auth/layout problem, where retrying
    will not help. ``resp.reason`` can be None (no reason phrase on the status
    line), so the detail is rebuilt with whitespace collapsed rather than
    printing a literal "None" or a double space.
    """
    resp = exc.response
    if resp is None:
        status = None
        detail = str(exc)
    else:
        status = resp.status_code
        detail = " ".join(f"HTTP {status} {resp.reason or ''} for {resp.url}".split())
    if status is None or status in (403, 429):
        return (
            f"Error: the CHIRPS data server refused the request ({detail}). "
            "This usually means rate limiting / a temporary IP block from "
            "too many requests — wait and retry later, and consider "
            f"lowering --workers (current: {workers})."
        )
    return (
        f"Error: the CHIRPS data server refused the request ({detail}). "
        f"This looks like a request/auth/layout problem (HTTP {status}); "
        "retrying will not help — check that the CHIRPS product layout "
        "has not changed."
    )


def _download_day_tif(session: requests.Session, day: date, dest_dir: Path) -> Path:
    """Fetch one day, preferring the validated final product over prelim.

    Try the final `sat` URL first. ANY final-side failure — a 404 (not finalized
    yet), a transient 5xx, an auth/throttle 4xx, or a corrupt 200 — falls through
    to the prelim URL; only if prelim ALSO fails is the day unavailable. This
    keeps a final-server glitch from dropping a day prelim can serve, or aborting
    the whole run on a final-only 4xx. The day is written to a temp file named
    after whichever product served it; the post-loop classifier decides
    tail-vs-mid-gap from the days that ultimately had no data anywhere.
    """
    final_name = f"chirps-v3.0.sat.{day.year:04d}.{day.month:02d}.{day.day:02d}.tif"
    final_url = f"{CHIRPS_FINAL_BASE_URL}/{day.year:04d}/{final_name}"
    prelim_name = f"chirps-v3.0.prelim.{day.year:04d}.{day.month:02d}.{day.day:02d}.tif"
    prelim_url = f"{CHIRPS_PRELIM_BASE_URL}/{day.year:04d}/{prelim_name}"

    # Final 404 yields the _NOT_FOUND sentinel; any other final-side failure
    # (DayUnavailable for 5xx/transient/corrupt, HTTPError for other-4xx) is
    # caught and also routed to the prelim fallback. Prelim's own result then
    # stands: a prelim failure propagates normally (DayUnavailable -> the day is
    # classified missing; HTTPError -> a real site-wide config problem aborts).
    try:
        body = _get_tif_body(session, final_url)
    except (DayUnavailable, requests.HTTPError):
        body = _NOT_FOUND
    name = final_name
    if body is _NOT_FOUND:
        body = _get_tif_body(session, prelim_url)
        name = prelim_name
        if body is _NOT_FOUND:
            raise DayUnavailable(
                f"day unavailable from final ({final_url}) or prelim ({prelim_url})"
            )

    out = dest_dir / name
    with open(out, "wb") as f:
        f.write(body)
    return out


def _discover_latest() -> date:
    """Find the newest available CHIRPS prelim day on or before today (UTC) — the
    `latest` resolver for CHIRPS.

    Probes backward day-by-day over HTTPS from today, classifying availability
    the same way an actual download (``_download_day_tif``) would see it:
      - HTTP 200 means the day is available -> return it;
      - HTTP 404 means not-yet-published -> step back one day;
      - HTTP 5xx or a transport-level failure is transient -> step back, but
        remembered: if the probe never reaches a definitive 200/404 answer
        (every day failed transiently), that is a connectivity/server problem,
        not "no data", and is surfaced as a real error;
      - any other status (403/401/405/other-4xx) is surfaced as a real
        config/auth problem.
    Each day is probed with HEAD first (cheap — no body), but because
    ``_download_day_tif`` issues a GET, a server that rejects HEAD (e.g. 405
    Method Not Allowed, or a 403/other-4xx that only applies to HEAD) would
    falsely abort here even though the day downloads fine. So a non-404 HEAD
    answer (the 405/403/other-4xx case) is re-probed with GET before deciding:
    the GET status is then what a real download would see, keeping availability
    detection consistent with the downloader. Bounded by
    ``_LATEST_LOOKBACK_DAYS`` (covers the product's worst-case pentad lag plus
    margin). Exits 2 if no day is available, distinguishing a genuine
    not-yet-published lookback from a persistent transport/server failure.
    """
    today = datetime.now(UTC).date()
    session = requests.Session()
    transient_only = True  # cleared as soon as any probe gets a definitive 200/404
    last_transient = None

    def _probe_status(url: str):
        """Return (status_code, None) or (None, transient_message) for one day.

        HEAD first; on any non-404, non-200, non-5xx answer (405/403/other-4xx)
        re-issue a GET so the verdict matches what the downloader's GET sees.
        A transport-level failure on either request is reported as transient.
        """
        try:
            resp = session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            return None, f"transport error: {exc}"
        if resp.status_code in (200, 404) or resp.status_code >= 500:
            return resp.status_code, None
        # 4xx other than 404 (403/401/405/bad request, etc.) on HEAD may be a
        # HEAD-specific rejection; confirm with a GET, which is what the real
        # download issues, before treating it as a config/auth problem.
        try:
            get_resp = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            return None, f"transport error: {exc}"
        return get_resp.status_code, None

    try:
        for offset in range(_LATEST_LOOKBACK_DAYS + 1):
            day = today - timedelta(days=offset)
            name = f"chirps-v3.0.prelim.{day.year:04d}.{day.month:02d}.{day.day:02d}.tif"
            url = f"{CHIRPS_PRELIM_BASE_URL}/{day.year:04d}/{name}"
            status, transient = _probe_status(url)
            if status is None:
                # Transport-level failure on this probe day is transient: step
                # back, but record it so an all-transient probe surfaces below
                # instead of being misreported as "no data".
                last_transient = transient
                continue
            if status == 200:
                return day
            if status == 404:
                # Not yet published: a definitive answer, step back one day.
                transient_only = False
                continue
            if status >= 500:
                # Server-side error is transient: step back, remembering it.
                last_transient = f"HTTP {status}"
                continue
            # 4xx other than 404 (403/401/bad request, etc.) confirmed by GET is
            # a real config or auth problem, not a not-yet-published day; surface
            # it immediately.
            print(
                f"Error: CHIRPS 'latest' probe got HTTP {status} for {url}; "
                "this is a config/auth problem, not a not-yet-published day.",
                file=sys.stderr,
            )
            sys.exit(2)
    finally:
        session.close()
    if transient_only and last_transient is not None:
        print(
            f"Error: CHIRPS 'latest' probe never reached the data server over the "
            f"last {_LATEST_LOOKBACK_DAYS} days (last failure: {last_transient}); "
            "this is a connectivity/server problem, not a not-yet-published day.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"Error: no CHIRPS prelim day available in the last {_LATEST_LOOKBACK_DAYS} "
        f"days (probed back to {(today - timedelta(days=_LATEST_LOOKBACK_DAYS)).isoformat()}); "
        "cannot resolve 'latest'.",
        file=sys.stderr,
    )
    sys.exit(2)


def _open_day(tif: Path, day: date) -> xr.DataArray:
    da = rioxarray.open_rasterio(tif, masked=False).squeeze("band", drop=True)
    da = da.where(da != CHIRPS_NODATA)
    da = da.rename({"y": "latitude", "x": "longitude"})
    if "spatial_ref" in da.coords:
        da = da.drop_vars("spatial_ref")
    da.attrs = {}
    da = da.expand_dims(time=[np.datetime64(day.isoformat(), "ns")])
    return da


def _stamp_cf_attrs(ds: xr.Dataset) -> xr.Dataset:
    if "latitude" in ds.coords:
        ds["latitude"].attrs.setdefault("standard_name", "latitude")
        ds["latitude"].attrs.setdefault("units", "degrees_north")
        ds["latitude"].attrs.setdefault("axis", "Y")
    if "longitude" in ds.coords:
        ds["longitude"].attrs.setdefault("standard_name", "longitude")
        ds["longitude"].attrs.setdefault("units", "degrees_east")
        ds["longitude"].attrs.setdefault("axis", "X")
    if "time" in ds.coords:
        ds["time"].attrs.setdefault("standard_name", "time")
        ds["time"].attrs.setdefault("axis", "T")
    return ds


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_SKILL_VERSION}",
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
            f"Max concurrent per-day download threads (default {DEFAULT_WORKERS}). "
            "Deliberately conservative: CHC's data server can throttle and "
            "temporarily block IPs under higher concurrency."
        ),
    )
    args = p.parse_args()

    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        sys.exit(2)

    # Resolve --start/--end to concrete inclusive dates. Malformed tokens and
    # post-resolution reversed ranges exit 2 before any fetch. `latest` triggers
    # a backward HTTPS probe (the resolution itself, run at most once); an
    # all-absolute or now-only window performs no discovery. An absolute
    # YYYY-MM-DD endpoint normalizes through date.fromisoformat, so the resolved
    # isoformat is byte-identical to the raw input — absolute behavior unchanged.
    start_date, end_date, log_line = _resolve_window(args.start, args.end, _discover_latest)
    start = start_date.isoformat()
    end = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    # --workers is a concurrency knob, not a data parameter, so it is excluded
    # from the cache key: the same {start, end} request at any worker count
    # produces the same data. The key records the RESOLVED concrete window, never
    # the relative token.
    requested_entry = {
        "skill": "chirps-fetch",
        "version": _SKILL_VERSION,
        "args": {"start": start, "end": end},
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, requested_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    print(f"Fetching CHIRPS {start} -> {end} (final product, prelim fallback)", file=sys.stderr)

    expected_days = list(_daterange(start, end))
    succeeded: list[tuple[date, xr.DataArray]] = []
    missing_days: list[date] = []
    # HTTP status (or None) of each missing day's DayUnavailable, keyed by day;
    # lets the all-missing classifier recognize an all-5xx site-wide refusal.
    missing_status: dict[date, int | None] = {}

    with tempfile.TemporaryDirectory(prefix="chirps_") as tmpdir:
        tmp = Path(tmpdir)
        # Only the downloads run concurrently. Each worker returns its day's tif
        # path on success or signals a missing day via DayUnavailable; the tifs
        # are opened with _open_day SEQUENTIALLY in the main thread after the
        # pool drains. The download is the network-I/O bottleneck, while
        # _open_day is fast and GDAL concurrent-open safety is build-dependent —
        # so parallelizing only the download captures the win without relying on
        # unverified concurrent-open behavior.
        sessions = _SessionPool()

        def _download(day: date) -> tuple[date, Path]:
            return day, _download_day_tif(sessions.session(), day, tmp)

        downloaded: list[tuple[date, Path]] = []
        try:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(_download, day): day for day in expected_days}
                for fut in as_completed(futures):
                    day = futures[fut]
                    try:
                        # A DayUnavailable (404/5xx/transient/truncated/non-TIFF)
                        # marks the day missing. ANY requests.HTTPError escaping
                        # a worker (today that is the prelim-side non-404 4xx
                        # raise — a final-side HTTPError is swallowed by the
                        # prelim fallback — but the clause is not limited to
                        # that by construction) means the server is refusing
                        # requests, so the run aborts with a clean message. Any
                        # other exception is unexpected and is re-raised by
                        # future.result(), so the run fails loudly rather than
                        # silently dropping a day or hanging the pool.
                        result_day, tif = fut.result()
                    except DayUnavailable as e:
                        print(
                            f"  {day.isoformat()}: day unavailable ({e}); "
                            "will classify after download",
                            file=sys.stderr,
                        )
                        missing_days.append(day)
                        missing_status[day] = e.status
                        continue
                    except requests.HTTPError as e:
                        # Cancel not-yet-started downloads so no new request
                        # starts while the abort message is built and printed.
                        pool.shutdown(wait=False, cancel_futures=True)
                        print(_http_refusal_message(e, args.workers), file=sys.stderr)
                        sys.stderr.flush()
                        # os._exit avoids the ThreadPoolExecutor __exit__ /
                        # shutdown(wait=True) that a SystemExit would trigger,
                        # which would block on in-flight requests. The trade-off
                        # is that it also skips the TemporaryDirectory context
                        # manager's cleanup, so the temp dir is left on disk for
                        # the OS's normal temp-cleanup (tmp reaper / reboot) to
                        # reclaim later, not reclaimed at exit.
                        os._exit(2)
                    print(f"  {result_day.isoformat()}", file=sys.stderr)
                    downloaded.append((result_day, tif))
        finally:
            sessions.close_all()

        # Restore day order on both outcome lists. Futures complete in an
        # arbitrary order, but the classifier below compares `missing_days`
        # against the day-sorted `expected_tail` as lists and takes
        # `succeeded_days[-1]` as the last available day, so both must be sorted
        # to reproduce the serial path's behavior exactly.
        missing_days.sort()
        for day, tif in sorted(downloaded, key=lambda dt: dt[0]):
            succeeded.append((day, _open_day(tif, day)))

        # Classify outcome.
        if not succeeded:
            # When EVERY missing day failed with a 5xx, the server refused the
            # whole run (likely throttling) — not a data gap, so say so instead
            # of the genuinely-absent-data diagnostic below. This branch runs
            # after the pool has fully drained, so a normal sys.exit is fine.
            statuses = [missing_status.get(d) for d in missing_days]
            if statuses and all(s is not None and s >= 500 for s in statuses):
                codes = ", ".join(str(c) for c in sorted(set(statuses)))
                print(
                    f"Error: the CHIRPS data server refused every request in "
                    f"range {start}..{end} (HTTP {codes} on all days). This "
                    "usually means rate limiting / a temporary server-side "
                    "block from too many requests — wait and retry later, and "
                    f"consider lowering --workers (current: {args.workers}).",
                    file=sys.stderr,
                )
                sys.exit(2)
            print(
                f"Error: no days available in range {start}..{end} from the "
                f"CHIRPS data server (final or prelim sat product). CHIRPS v3.0 "
                f"sat coverage runs {CHIRPS_FINAL_START_YEAR}-to-present, so a "
                "date before that range yields nothing; otherwise this is a "
                "server-side data gap. The validated final product also lags, so "
                "very recent days come from the preliminary product (published 2 "
                "days after each pentad closes — pentads end on days 5, 10, 15, "
                "20, 25, and last of month, worst-case lag ~7 days); for a very "
                "recent --end, try an earlier one.",
                file=sys.stderr,
            )
            sys.exit(2)

        succeeded_days = [d for d, _ in succeeded]
        last_succeeded = succeeded_days[-1]
        expected_tail = [d for d in expected_days if d > last_succeeded]
        if missing_days and missing_days != expected_tail:
            # Mid-range gap: some missing day precedes a succeeded day.
            print(
                f"Non-tail missing day(s) {', '.join(d.isoformat() for d in missing_days)} "
                f"— server-side data gap, not a lag issue. "
                "Refusing to write a partial zarr with a hole in the middle.",
                file=sys.stderr,
            )
            sys.exit(2)

        # At this point: either no missing days (full success) or
        # missing_days is exactly the contiguous tail past last_succeeded.
        effective_end = last_succeeded.isoformat()
        if missing_days:
            print(
                f"Tail-missing day(s) {', '.join(d.isoformat() for d in missing_days)}; "
                f"writing partial zarr with effective end {effective_end} "
                f"(requested --end was {end}). "
                "Consistent with CHIRPS v3.0 preliminary's pentad-based "
                "schedule (per-day files published 2 days after each pentad "
                "ends on days 5, 10, 15, 20, 25, and last of month; "
                "worst-case lag ~7 days).",
                file=sys.stderr,
            )

        # Cache stamp reflects the EFFECTIVE end actually written, so a re-run
        # against the same --end re-attempts the missing tail days instead of
        # short-circuiting on a cache hit. Built before the write loop because
        # the first day's provenance stamp needs the effective entry.
        effective_entry = {
            **requested_entry,
            "args": {"start": start, "end": effective_end},
        }

        # Stream each day to zarr one at a time so peak resident memory is
        # bounded to ~one day regardless of window length, instead of holding
        # the whole concatenated window in RAM. `succeeded` is already
        # day-sorted (built sorted above), so the appended time axis stays
        # ascending. Per-day latitude-sort is equivalent to a single global
        # sort because every CHIRPS day shares the identical latitude grid.
        for i, (_, da) in enumerate(succeeded):
            da = da.sortby("latitude", ascending=True)
            da.name = "precip"
            da.attrs["units"] = "mm day-1"
            da.attrs["standard_name"] = "lwe_precipitation_rate"
            da.attrs["long_name"] = "CHIRPS daily precipitation"

            ds = da.to_dataset()
            # Stamp the root provenance/source + CF attrs on EVERY write. A
            # to_zarr(mode="a", append_dim="time") call rewrites the root group
            # attrs from the dataset being appended, so stamping only the first
            # write would clobber weather_skills_source/weather_skills_history to empty on the
            # first append. The effective entry is identical for every day, so
            # the final stamp is stable regardless of how many days are written.
            ds.attrs["weather_skills_source"] = "chirps"
            ds.attrs["weather_skills_history"] = json.dumps([effective_entry], sort_keys=True)
            ds.attrs["Conventions"] = "CF-1.13"
            _stamp_cf_attrs(ds)
            for v in ds.variables:
                ds[v].encoding = {}

            if i == 0:
                # Only the store creation is first-iteration work; the attrs
                # above are re-stamped on every append.
                if out.exists():
                    shutil.rmtree(out)
                out.parent.mkdir(parents=True, exist_ok=True)
                ds.to_zarr(out, mode="w", consolidated=True)
            else:
                ds.to_zarr(out, mode="a", append_dim="time", consolidated=True)
        print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
