# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "requests",
#   "xarray",
#   "zarr",
#   "numpy",
#   "rioxarray",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch CHIRPS precipitation from the public GCS mirror (final product, prelim fallback) and write a weather-skills standard dataset Zarr."""

import os
import re
import sys
import tempfile
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.units import stamp_data_interval, to_standard_units

_BUCKET = "sheerwater-public-datalake"
_MIRROR = "chc-mirror"
_GCS_MEDIA = f"https://storage.googleapis.com/{_BUCKET}"
_GCS_API = f"https://storage.googleapis.com/storage/v1/b/{_BUCKET}/o"
_CHIRPS_FINAL_PREFIX = f"{_MIRROR}/products/CHIRPS/v3.0/daily/final/sat"
_CHIRPS_PRELIM_PREFIX = f"{_MIRROR}/products/CHIRPS/v3.0/daily/prelim/sat"
CHIRPS_FINAL_START_YEAR = 1998
CHIRPS_NODATA = -9999.0
HTTP_TIMEOUT = 60
DEFAULT_WORKERS = 8

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_NOT_FOUND = object()
_TIF_NAME_RE = re.compile(r"chirps-v3\.0\.(?:prelim|sat)\.(\d{4})\.(\d{2})\.(\d{2})\.tif")


def _object_url(key: str) -> str:
    return f"{_GCS_MEDIA}/{urllib.parse.quote(key, safe='/')}"


def _list_object_names(prefix: str) -> list[str]:
    """Object names under ``prefix`` on the public CHC mirror (paginated)."""
    import requests

    names: list[str] = []
    token = None
    while True:
        params = {"prefix": prefix, "maxResults": 1000}
        if token:
            params["pageToken"] = token
        try:
            resp = requests.get(_GCS_API, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise DataError(f"CHIRPS mirror listing failed for {prefix}: {exc}") from None
        if resp.status_code != 200:
            raise DataError(
                f"CHIRPS mirror listing failed: HTTP {resp.status_code} for prefix {prefix}"
            )
        payload = resp.json()
        for item in payload.get("items") or []:
            name = item.get("name")
            if isinstance(name, str) and not name.endswith("/"):
                names.append(name)
        token = payload.get("nextPageToken")
        if not token:
            break
    return names


class DayUnavailable(Exception):
    """Day's TIF unavailable (404, 5xx, truncated/empty/non-TIFF). ``status`` is HTTP code when known."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _SessionPool:
    """Per-thread requests.Session (Session is not thread-safe)."""

    def __init__(self):
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()

    def session(self):
        import requests

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


def _get_tif_body(session, url: str):
    """Fetch one day TIF. Returns bytes, ``_NOT_FOUND`` on 404, or raises DayUnavailable."""
    import requests

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
    if body[:4] not in (b"II*\x00", b"MM\x00*"):
        raise DayUnavailable(f"non-TIFF body for {url}: leading bytes {body[:4]!r}")
    return body


def _http_refusal_message(exc, workers: int) -> str:
    resp = exc.response
    if resp is None:
        status = None
        detail = str(exc)
    else:
        status = resp.status_code
        detail = " ".join(f"HTTP {status} {resp.reason or ''} for {resp.url}".split())
    if status is None or status in (403, 429):
        return (
            f"Error: the CHIRPS mirror refused the request ({detail}). "
            "Wait and retry later, and consider lowering --workers "
            f"(current: {workers})."
        )
    return (
        f"Error: the CHIRPS mirror refused the request ({detail}). "
        f"This looks like a request/auth/layout problem (HTTP {status}); "
        "retrying will not help — check that the CHIRPS product layout "
        "has not changed."
    )


def _download_day_tif(session, day: date, dest_dir: Path) -> Path:
    """Prefer final product; fall through to prelim on any final-side failure."""
    import requests

    final_name = f"chirps-v3.0.sat.{day.year:04d}.{day.month:02d}.{day.day:02d}.tif"
    final_url = _object_url(f"{_CHIRPS_FINAL_PREFIX}/{day.year:04d}/{final_name}")
    prelim_name = f"chirps-v3.0.prelim.{day.year:04d}.{day.month:02d}.{day.day:02d}.tif"
    prelim_url = _object_url(f"{_CHIRPS_PRELIM_PREFIX}/{day.year:04d}/{prelim_name}")

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


def _open_day(tif: Path, day: date):
    import numpy as np
    import rioxarray

    da = rioxarray.open_rasterio(tif, masked=False).squeeze("band", drop=True)
    da = da.where(da != CHIRPS_NODATA)
    da = da.rename({"y": "latitude", "x": "longitude"})
    if "spatial_ref" in da.coords:
        da = da.drop_vars("spatial_ref")
    da.attrs = {}
    return da.expand_dims(time=[np.datetime64(day.isoformat(), "ns")])


@weather_skill(
    name="chirps-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"Max concurrent per-day download threads (default {DEFAULT_WORKERS}).",
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
def fetch(start_time, end_time, workers, **kwargs):
    """Fetch CHIRPS precipitation from the public GCS mirror (final product, prelim fallback) and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        found: list[date] = []
        today = date.today()
        for year in (today.year, today.year - 1):
            for prefix in (_CHIRPS_PRELIM_PREFIX, _CHIRPS_FINAL_PREFIX):
                for name in _list_object_names(f"{prefix}/{year:04d}/"):
                    match = _TIF_NAME_RE.fullmatch(name.rsplit("/", 1)[-1])
                    if match:
                        found.append(
                            date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                        )
            if found:
                break
        if not found:
            raise DataError("CHIRPS probe found no daily TIF names in the year directories")
        print(max(found).isoformat())
        return

    import requests

    start = start_time.isoformat()
    end = end_time.isoformat()
    print(f"Fetching CHIRPS {start} -> {end} (final product, prelim fallback)", file=sys.stderr)

    expected_days = [
        start_time + timedelta(days=i) for i in range((end_time - start_time).days + 1)
    ]
    succeeded = []
    missing_days: list[date] = []
    missing_status: dict[date, int | None] = {}

    with tempfile.TemporaryDirectory(prefix="chirps_") as tmpdir:
        tmp = Path(tmpdir)
        sessions = _SessionPool()

        def _download(day: date) -> tuple[date, Path]:
            return day, _download_day_tif(sessions.session(), day, tmp)

        downloaded: list[tuple[date, Path]] = []
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_download, day): day for day in expected_days}
                for fut in as_completed(futures):
                    day = futures[fut]
                    try:
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
                        pool.shutdown(wait=False, cancel_futures=True)
                        print(_http_refusal_message(e, workers), file=sys.stderr)
                        sys.stderr.flush()
                        # Skip ThreadPoolExecutor/tempdir cleanup to avoid blocking
                        # on in-flight requests; OS reclaims the temp dir later.
                        os._exit(2)
                    print(f"  {result_day.isoformat()}", file=sys.stderr)
                    downloaded.append((result_day, tif))
        finally:
            sessions.close_all()

        missing_days.sort()
        for day, tif in sorted(downloaded, key=lambda dt: dt[0]):
            succeeded.append((day, _open_day(tif, day)))

        if not succeeded:
            statuses = [missing_status.get(d) for d in missing_days]
            if statuses and all(s is not None and s >= 500 for s in statuses):
                codes = ", ".join(str(c) for c in sorted(set(statuses)))
                raise UsageError(
                    f"the CHIRPS mirror refused every request in "
                    f"range {start}..{end} (HTTP {codes} on all days). "
                    "Wait and retry later, and consider lowering --workers "
                    f"(current: {workers})."
                )
            raise UsageError(
                f"no days available in range {start}..{end} from the "
                f"CHIRPS mirror (final or prelim sat product). CHIRPS v3.0 "
                f"sat coverage runs {CHIRPS_FINAL_START_YEAR}-to-present, so a "
                "date before that range yields nothing; otherwise this is a "
                "data gap. The validated final product also lags, so "
                "very recent days come from the preliminary product (published 2 "
                "days after each pentad closes — pentads end on days 5, 10, 15, "
                "20, 25, and last of month, worst-case lag ~7 days); for a very "
                "recent --end-time, try an earlier one."
            )

        succeeded_days = [d for d, _ in succeeded]
        last_succeeded = succeeded_days[-1]
        expected_tail = [d for d in expected_days if d > last_succeeded]
        if missing_days and missing_days != expected_tail:
            raise UsageError(
                f"Non-tail missing day(s) {', '.join(d.isoformat() for d in missing_days)} "
                f"— server-side data gap, not a lag issue. "
                "Refusing to write a partial zarr with a hole in the middle.",
                prefix=False,
            )

        effective_end = last_succeeded.isoformat()
        if missing_days:
            print(
                f"Tail-missing day(s) {', '.join(d.isoformat() for d in missing_days)}; "
                f"writing partial dataset with effective end {effective_end} "
                f"(requested --end-time was {end}). "
                "Consistent with CHIRPS v3.0 preliminary's pentad-based schedule.",
                file=sys.stderr,
            )

        import xarray as xr

        pieces = []
        for _, da in succeeded:
            da = da.sortby("latitude", ascending=True)
            da.name = "precip"
            da.attrs["units"] = "mm day-1"
            da.attrs["standard_name"] = "lwe_precipitation_rate"
            da.attrs["long_name"] = "CHIRPS daily precipitation"
            pieces.append(da)

        ds = xr.concat(pieces, dim="time").to_dataset()
        ds.attrs["Conventions"] = "CF-1.13"
        ds.attrs["weather_skills_source"] = "chirps"
        stamp_cf_attrs(ds)
        ds = to_standard_units(ds, variables=["precip"])
        return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()
