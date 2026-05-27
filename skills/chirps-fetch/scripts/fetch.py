# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
#   "rioxarray",
# ]
# ///
"""Fetch CHIRPS prelim precipitation from FTP and write a Rhiza Envelope Zarr."""

import argparse
import ftplib
import json
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import rioxarray  # noqa: F401 — registers .rio accessor
import xarray as xr

CHIRPS_FTP_HOST = "ftp.chc.ucsb.edu"
CHIRPS_FTP_DIR = "/pub/org/chc/products/CHIRPS/v3.0/daily/prelim/sat"
CHIRPS_NODATA = -9999.0


class DayUnavailable(Exception):
    """Raised when a specific day's TIF is not yet published on the FTP server (550)."""


# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.2"


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


def _download_day_tif(ftp: ftplib.FTP, day: date, dest_dir: Path) -> Path:
    name = f"chirps-v3.0.prelim.{day.year:04d}.{day.month:02d}.{day.day:02d}.tif"
    # A cwd failure here indicates a real config problem (missing year dir,
    # auth issue, etc.) — let ftplib.error_perm propagate.
    ftp.cwd(f"{CHIRPS_FTP_DIR}/{day.year:04d}")
    out = dest_dir / name
    try:
        with open(out, "wb") as f:
            ftp.retrbinary(f"RETR {name}", f.write)
    except ftplib.error_perm as exc:
        # Only 550 means "file not found"; 530/553 etc. are real errors and
        # should propagate as ftplib.error_perm.
        if str(exc).startswith("550"):
            raise DayUnavailable(f"not posted: {exc}") from exc
        raise
    except (ftplib.error_temp, EOFError, ConnectionError, TimeoutError, OSError) as exc:
        # FTP 4xx temporary failures, mid-stream server hangups, socket
        # disconnects / timeouts: treat the day as missing so the post-loop
        # classifier handles tail-vs-mid-gap the same way it does for 550s.
        raise DayUnavailable(f"transient network error: {exc}") from exc
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
    args = p.parse_args()

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
        ftp = ftplib.FTP(CHIRPS_FTP_HOST, timeout=60)
        ftp.login()
        try:
            for day in expected_days:
                print(f"  {day.isoformat()}", file=sys.stderr)
                try:
                    tif = _download_day_tif(ftp, day, tmp)
                except DayUnavailable as e:
                    print(
                        f"    day unavailable ({e}); will classify after loop",
                        file=sys.stderr,
                    )
                    missing_days.append(day)
                    continue
                succeeded.append((day, _open_day(tif, day)))
        finally:
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()

        # Classify outcome.
        if not succeeded:
            print(
                f"Error: no days available in range {args.start}..{args.end} "
                f"on the CHIRPS FTP server. CHIRPS v3.0 preliminary is "
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
