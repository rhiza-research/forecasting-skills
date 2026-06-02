# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "earthaccess",
#   "h5netcdf",
#   "h5py",
#   "dask",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch IMERG live precipitation and write a Rhiza Envelope Zarr."""

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.1"

SHORTNAMES = {
    "late": "GPM_3IMERGDL",
    "final": "GPM_3IMERGDF",
}

# Extra calendar days searched before the requested window start when
# auto-discovering the end granule, so a production lag of up to two weeks
# still surfaces a granule on or before the anchor.
_DISCOVERY_LOOKBACK_PAD_DAYS = 14

_LAST_RE = re.compile(r"^(?P<n>\d+)(?P<unit>[dw])$")


def _parse_last(spec: str) -> int:
    """Parse a relative-window spec into an inclusive calendar-day count.

    Accepts ``<int>d`` (days) or ``<int>w`` (weeks, where 1w = 7 days). The
    integer must be >= 1. ``"3w"`` -> 21, ``"21d"`` -> 21. Anything else
    (e.g. ``"3weeks"``, ``"0d"``, ``"-1d"``, ``"d"``) raises ValueError.
    """
    m = _LAST_RE.match(spec)
    if m is None:
        raise ValueError(f"invalid --last value {spec!r}: expected <int>d or <int>w (e.g. 21d, 3w)")
    n = int(m.group("n"))
    if n < 1:
        raise ValueError(f"invalid --last value {spec!r}: count must be >= 1")
    return n * 7 if m.group("unit") == "w" else n


def _resolve_window(end_date: date, n_days: int) -> date:
    """Return the inclusive start date for a window of ``n_days`` ending at
    ``end_date``. The window spans ``n_days`` calendar days inclusive, so
    ``start = end - (n_days - 1)``."""
    return end_date - timedelta(days=n_days - 1)


def _resolve_anchor(anchor: str) -> date:
    """Resolve the ``--anchor`` value (``"today"`` or an ISO date) to a date."""
    if anchor == "today":
        return date.today()
    return date.fromisoformat(anchor)


def _load_history(zarr_path: Path) -> list:
    import xarray as xr

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


def _stamp_cf_attrs(ds):
    """Stamp CF standard_name/units/axis on spatial + time coords (non-destructive)."""
    for name in ("latitude", "lat", "y"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "latitude")
            ds[name].attrs.setdefault("units", "degrees_north")
            ds[name].attrs.setdefault("axis", "Y")
            break
    for name in ("longitude", "lon", "x"):
        if name in ds.coords:
            ds[name].attrs.setdefault("standard_name", "longitude")
            ds[name].attrs.setdefault("units", "degrees_east")
            ds[name].attrs.setdefault("axis", "X")
            break
    if "time" in ds.coords:
        ds["time"].attrs.setdefault("standard_name", "time")
        ds["time"].attrs.setdefault("axis", "T")
    return ds


def _granule_date(result) -> date:
    """Extract the start date of an earthaccess granule's temporal coverage."""
    iso = result["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]
    # BeginningDateTime is an ISO 8601 instant (e.g. "2026-05-29T00:00:00.000Z");
    # the leading 10 chars are the calendar date.
    return date.fromisoformat(iso[:10])


def _discover_end(shortname: str, anchor: date, n_days: int):
    """Find the latest granule on or before ``anchor`` and return its date with
    the search results, so the caller can reuse them for the actual fetch.

    Searches a bounded lookback ending at ``anchor`` (wide enough to span the
    requested window plus a production-lag pad), then returns the max granule
    date ``<= anchor``. Returns ``(end_date, results)``. Exits 2 if no granule
    falls on or before ``anchor`` in that window.
    """
    import earthaccess

    lookback_start = anchor - timedelta(days=n_days + _DISCOVERY_LOOKBACK_PAD_DAYS)
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(lookback_start.isoformat(), anchor.isoformat()),
    )
    on_or_before = [r for r in results if _granule_date(r) <= anchor]
    if not on_or_before:
        print(
            f"Error: no granules on or before anchor {anchor.isoformat()} "
            f"for IMERG {shortname} in lookback window "
            f"{lookback_start.isoformat()}..{anchor.isoformat()}.",
            file=sys.stderr,
        )
        sys.exit(2)
    end_date = max(_granule_date(r) for r in on_or_before)
    return end_date, on_or_before


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--end", help="End date YYYY-MM-DD (inclusive)")
    p.add_argument(
        "--last",
        help=(
            "Relative window length as <int>d or <int>w (e.g. 21d, 3w). "
            "Mutually exclusive with --start/--end. The window end is the "
            "latest available granule on or before --anchor; start is end - "
            "(N-1) inclusive days."
        ),
    )
    p.add_argument(
        "--anchor",
        default="today",
        help=(
            "Upper bound for --last end-granule discovery: 'today' (default) "
            "or an ISO date YYYY-MM-DD. Ignored without --last."
        ),
    )
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--version", default="late", choices=list(SHORTNAMES))
    args = p.parse_args()

    # Mode selection: --last XOR (--start AND --end). Reject before any
    # network/login call so an LLM caller gets a fast, clear failure.
    if args.last is not None and (args.start is not None or args.end is not None):
        print(
            "Error: --last is mutually exclusive with --start/--end; pass one mode only.",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.last is None and (args.start is None or args.end is None):
        print(
            "Error: specify a window: either --last <N>d|w, or both --start and --end.",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.last is not None:
        try:
            n_days = _parse_last(args.last)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        try:
            anchor_date = _resolve_anchor(args.anchor)
        except ValueError as exc:
            print(
                f"Error: invalid --anchor value {args.anchor!r}: "
                f"expected 'today' or an ISO date YYYY-MM-DD ({exc}).",
                file=sys.stderr,
            )
            sys.exit(2)

    shortname = SHORTNAMES[args.version]
    out = Path(args.output)

    # Heavy deps are imported only past the arg-validation guards above, so the
    # mutual-exclusion / neither-mode / malformed-`--last` error paths exit 2
    # without requiring earthaccess or xarray to be installed.
    import earthaccess
    import xarray as xr

    if args.last is not None:
        # Discovery needs login. Run it first, then echo the resolved window
        # to stderr before any download.
        earthaccess.login()
        end_date, results = _discover_end(shortname, anchor_date, n_days)
        start_date = _resolve_window(end_date, n_days)
        start = start_date.isoformat()
        end = end_date.isoformat()
        print(
            f"resolved --last {args.last} (anchor={anchor_date.isoformat()}) -> "
            f"{start}..{end} ({n_days} days)",
            file=sys.stderr,
        )
        # Reuse the discovery search's results: keep only granules within the
        # resolved [start, end] window rather than issuing a second search.
        results = [r for r in results if start_date <= _granule_date(r) <= end_date]
    else:
        start = args.start
        end = args.end
        results = None

    # Cache/provenance args record the resolved concrete window, never the
    # relative --last/--anchor inputs, so the same resolved window cache-hits
    # and a relative spec never false-hits across days. Mirrors chirps-fetch's
    # explicit-args cache key.
    entry = {
        "skill": "imerg-fetch",
        "version": _RHIZA_SKILL_VERSION,
        "args": {"start": start, "end": end, "version": args.version},
        "input": None,
    }
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    print(
        f"Fetching IMERG {args.version} ({shortname}) {start} -> {end}",
        file=sys.stderr,
    )

    if results is None:
        # Absolute mode: search the requested window. The relative mode already
        # holds the granules from discovery.
        earthaccess.login()
        results = earthaccess.search_data(
            short_name=shortname,
            cloud_hosted=True,
            temporal=(start, end),
        )
    if not results:
        raise RuntimeError(f"No IMERG {args.version} granules found in {start}..{end}")
    print(f"Found {len(results)} granules", file=sys.stderr)

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="imerg-fetch-") as td:
        files = earthaccess.download(results, local_path=td)
        ds = xr.open_mfdataset(
            files,
            engine="h5netcdf",
            combine="by_coords",
        )
        ds = ds[["precipitation"]].rename({"precipitation": "precip"})
        # CMR's temporal filter is overlap-based and can return granules just
        # outside [start, end]; trim to exact requested bounds to match the
        # prior sheerwater @timeseries() post-process.
        ds = ds.sel(time=slice(start, end))
        ds = ds.drop_attrs()
        ds.attrs.update(
            rhiza_source="imerg",
            rhiza_history=json.dumps([entry], sort_keys=True),
        )
        ds["precip"].attrs.update(
            units="mm/day",
            standard_name="lwe_precipitation_rate",
            long_name="IMERG daily precipitation",
        )
        _stamp_cf_attrs(ds)
        for v in ds.variables:
            ds[v].encoding = {}

        # Materialize before the temp dir vanishes — open_mfdataset is lazy.
        ds.load().to_zarr(out, mode="w", consolidated=True)

    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
