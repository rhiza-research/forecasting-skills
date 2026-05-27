# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "requests",
#   "xarray",
#   "zarr",
#   "numpy",
#   "rioxarray",
# ]
# ///
"""Fetch CHIRPS prelim precipitation over HTTPS and write a Rhiza Envelope Zarr."""

import argparse
import json
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import requests
import rioxarray  # noqa: F401 — registers .rio accessor
import xarray as xr

CHIRPS_BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/prelim/sat"
CHIRPS_NODATA = -9999.0
HTTP_TIMEOUT = 60

# Default size of the per-day download thread pool. The work is
# network-I/O-bound (one independent HTTPS GET per day), so threads overlap
# request latency without contending on the GIL. The bound is deliberately
# conservative: CHC's data server is a public research host, so excessive
# concurrency is impolite and risks throttling. 8 is enough to hide request
# latency while staying well below levels that typically provoke rate limiting;
# operators can lower it with --workers if they observe throttling.
DEFAULT_WORKERS = 8


class DayUnavailable(Exception):
    """Raised when a day's TIF cannot be retrieved: not yet published (HTTP 404),
    a transient server (5xx) or network error, or a non-TIFF / truncated / empty
    body. The post-loop classifier then handles tail-vs-mid-gap."""


# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.3"


def _load_history(zarr_path: Path) -> list:
    try:
        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            raw = ds.attrs.get("rhiza_history")
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


def _download_day_tif(session: requests.Session, day: date, dest_dir: Path) -> Path:
    name = f"chirps-v3.0.prelim.{day.year:04d}.{day.month:02d}.{day.day:02d}.tif"
    url = f"{CHIRPS_BASE_URL}/{day.year:04d}/{name}"
    out = dest_dir / name
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        # Connection resets, timeouts, and other transport-level failures:
        # treat the day as missing so the post-loop classifier handles
        # tail-vs-mid-gap the same way it does for a 404.
        raise DayUnavailable(f"transient network error: {exc}") from exc

    if resp.status_code == 404:
        # The day's file is not yet published.
        raise DayUnavailable(f"not posted: HTTP 404 for {url}")
    if resp.status_code >= 500:
        # Server-side errors are transient: classify as a missing day so the
        # post-loop tail-vs-mid-gap logic handles them the same way as a 404.
        raise DayUnavailable(f"transient server error: HTTP {resp.status_code} for {url}")
    if resp.status_code != 200:
        # 4xx other than 404 (auth, bad request, etc.) indicates a real
        # config problem rather than a not-yet-published day; surface it.
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

    with open(out, "wb") as f:
        f.write(body)
    return out


def _open_day(tif: Path, day: date) -> xr.DataArray:
    da = rioxarray.open_rasterio(tif, masked=False).squeeze("band", drop=True)
    da = da.where(da != CHIRPS_NODATA)
    da = da.rename({"y": "lat", "x": "lon"})
    if "spatial_ref" in da.coords:
        da = da.drop_vars("spatial_ref")
    da.attrs = {}
    da = da.expand_dims(time=[np.datetime64(day.isoformat(), "ns")])
    return da


def _stamp_cf_attrs(ds: xr.Dataset) -> xr.Dataset:
    if "lat" in ds.coords:
        ds["lat"].attrs.setdefault("standard_name", "latitude")
        ds["lat"].attrs.setdefault("units", "degrees_north")
        ds["lat"].attrs.setdefault("axis", "Y")
    if "lon" in ds.coords:
        ds["lon"].attrs.setdefault("standard_name", "longitude")
        ds["lon"].attrs.setdefault("units", "degrees_east")
        ds["lon"].attrs.setdefault("axis", "X")
    if "time" in ds.coords:
        ds["time"].attrs.setdefault("standard_name", "time")
        ds["time"].attrs.setdefault("axis", "T")
    return ds


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            f"Max concurrent per-day download threads (default {DEFAULT_WORKERS}). "
            "Lower this if the CHIRPS server returns throttling errors."
        ),
    )
    args = p.parse_args()

    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        sys.exit(2)

    # --workers is a concurrency knob, not a data parameter, so it is excluded
    # from the cache key: the same {start, end} request at any worker count
    # produces the same data.
    requested_entry = {
        "skill": "chirps-fetch",
        "version": _RHIZA_SKILL_VERSION,
        "args": {"start": args.start, "end": args.end},
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, requested_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    print(f"Fetching CHIRPS prelim {args.start} -> {args.end}", file=sys.stderr)

    expected_days = list(_daterange(args.start, args.end))
    succeeded: list[tuple[date, xr.DataArray]] = []
    missing_days: list[date] = []

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
                        # marks the day missing. Any other exception is
                        # unexpected and is re-raised by future.result(), so the
                        # run fails loudly rather than silently dropping a day or
                        # hanging the pool.
                        result_day, tif = fut.result()
                    except DayUnavailable as e:
                        print(
                            f"  {day.isoformat()}: day unavailable ({e}); "
                            "will classify after download",
                            file=sys.stderr,
                        )
                        missing_days.append(day)
                        continue
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
            print(
                f"Error: no days available in range {args.start}..{args.end} "
                f"from the CHIRPS data server. CHIRPS v3.0 preliminary is "
                "published 2 days after each pentad closes (pentads end on "
                "days 5, 10, 15, 20, 25, and last of month), so worst-case "
                "lag is ~7 days; try an earlier --end.",
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
                f"(requested --end was {args.end}). "
                "Consistent with CHIRPS v3.0 preliminary's pentad-based "
                "schedule (per-day files published 2 days after each pentad "
                "ends on days 5, 10, 15, 20, 25, and last of month; "
                "worst-case lag ~7 days).",
                file=sys.stderr,
            )

        arrs = [da for _, da in succeeded]
        da = xr.concat(arrs, dim="time")
        da = da.sortby("lat", ascending=True)
        da.name = "precip"
        da.attrs["units"] = "mm/day"
        da.attrs["standard_name"] = "lwe_precipitation_rate"
        da.attrs["long_name"] = "CHIRPS daily precipitation"

        # Cache stamp reflects the EFFECTIVE end actually written, so a re-run
        # against the same --end re-attempts the missing tail days instead of
        # short-circuiting on a cache hit.
        effective_entry = {
            **requested_entry,
            "args": {"start": args.start, "end": effective_end},
        }

        ds = da.to_dataset()
        ds.attrs["rhiza_source"] = "chirps"
        ds.attrs["rhiza_history"] = json.dumps([effective_entry], sort_keys=True)
        _stamp_cf_attrs(ds)
        for v in ds.variables:
            ds[v].encoding = {}

        if out.exists():
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        ds.to_zarr(out, mode="w", consolidated=True)
        print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
