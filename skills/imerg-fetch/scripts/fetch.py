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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import earthaccess
import xarray as xr

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.1"

SHORTNAMES = {
    "late": "GPM_3IMERGDL",
    "final": "GPM_3IMERGDF",
}

# How far back from the anchor the end-discovery search looks for the latest
# available granule. This is independent of the requested window length: it only
# needs to span the product's realistic production lag (IMERG late runs a few
# days behind; final can run months behind), not the window. The actual window
# is fetched by a separate search over the exact resolved [start, end] (see
# main()), so a wide discovery lookback never inflates the fetched data.
_DISCOVERY_LOOKBACK_DAYS = 200

_LAST_RE = re.compile(r"^(?P<n>\d+)(?P<unit>[dw])$")

# Upper bound on the resolved day count for --last. IMERG begins in year 2000,
# so a window cannot meaningfully span more than ~26 years; 36525 days (~100
# years) is far beyond any real window yet small enough that
# `end - timedelta(days=n_days - 1)` cannot raise OverflowError. Rejecting above
# this cap keeps the failure pre-network instead of crashing in _resolve_window
# after login.
_MAX_LAST_DAYS = 36525


def _parse_last(spec: str) -> int:
    """Parse a relative-window spec into an inclusive calendar-day count.

    Accepts ``<int>d`` (days) or ``<int>w`` (weeks, where 1w = 7 days). The
    integer must be >= 1 and resolve to at most ``_MAX_LAST_DAYS`` days. ``"3w"``
    -> 21, ``"21d"`` -> 21. Anything else (e.g. ``"3weeks"``, ``"0d"``, ``"-1d"``,
    ``"d"``, or a count above the cap) raises ValueError.
    """
    m = _LAST_RE.match(spec)
    if m is None:
        raise ValueError(f"invalid --last value {spec!r}: expected <int>d or <int>w (e.g. 21d, 3w)")
    n = int(m.group("n"))
    if n < 1:
        raise ValueError(f"invalid --last value {spec!r}: count must be >= 1")
    n_days = n * 7 if m.group("unit") == "w" else n
    if n_days > _MAX_LAST_DAYS:
        raise ValueError(
            f"invalid --last value {spec!r}: resolves to {n_days} days, "
            f"above the maximum of {_MAX_LAST_DAYS} days (~100 years); "
            "IMERG data begins in year 2000, so no real window is this long"
        )
    return n_days


def _resolve_anchor(anchor: str) -> date:
    """Resolve the ``--anchor`` value (``"today"`` or an ISO date) to a date.

    ``"today"`` resolves to the current UTC date (not the local date), so the
    default anchor is deterministic for a UTC-based dataset regardless of the
    caller's timezone.
    """
    if anchor == "today":
        return datetime.now(UTC).date()
    return date.fromisoformat(anchor)


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


class GranuleShapeError(Exception):
    """Raised when a CMR granule result lacks the expected temporal-extent path
    or carries a non-ISO BeginningDateTime, so the failure surfaces as a clear
    message rather than an uncaught KeyError/ValueError traceback after login."""


def _granule_date(result) -> date:
    """Extract the start date of an earthaccess granule's temporal coverage.

    Raises GranuleShapeError if the expected
    ``umm.TemporalExtent.RangeDateTime.BeginningDateTime`` path is missing or
    its value is not a parseable ISO 8601 date, so a malformed CMR result yields
    a clear error rather than an uncaught traceback.
    """
    try:
        iso = result["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]
    except (KeyError, TypeError) as exc:
        raise GranuleShapeError(
            "CMR granule result missing "
            "umm.TemporalExtent.RangeDateTime.BeginningDateTime; "
            f"cannot determine its date (got: {result!r})"
        ) from exc
    # BeginningDateTime is an ISO 8601 instant (e.g. "2026-05-29T00:00:00.000Z");
    # the leading 10 chars are the calendar date.
    if not isinstance(iso, str):
        raise GranuleShapeError(f"CMR granule BeginningDateTime is not a string: {iso!r}")
    try:
        return date.fromisoformat(iso[:10])
    except ValueError as exc:
        raise GranuleShapeError(
            f"CMR granule BeginningDateTime {iso!r} is not a parseable ISO date"
        ) from exc


def _discover_end(shortname: str, anchor: date) -> date:
    """Find the latest available granule on or before ``anchor`` and return its
    date.

    Searches a lookback window ending at ``anchor`` whose width is set by the
    product's realistic production lag (``_DISCOVERY_LOOKBACK_DAYS``), not by the
    requested window length. The resolved window is fetched separately over its
    exact ``[start, end]`` (see ``main()``), so this discovery search only has to
    surface the most-recent granule. Returns the max granule date ``<= anchor``.
    Exits 2 if no granule falls on or before ``anchor`` in that lookback.
    """
    lookback_start = anchor - timedelta(days=_DISCOVERY_LOOKBACK_DAYS)
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
    return max(_granule_date(r) for r in on_or_before)


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
        default=None,
        help=(
            "Upper bound for --last end-granule discovery: 'today' (default) "
            "or an ISO date YYYY-MM-DD. Only valid with --last; passing it with "
            "--start/--end is an error."
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
        # In relative mode, an unset --anchor (None) means today.
        try:
            anchor_date = _resolve_anchor("today" if args.anchor is None else args.anchor)
        except ValueError as exc:
            print(
                f"Error: invalid --anchor value {args.anchor!r}: "
                f"expected 'today' or an ISO date YYYY-MM-DD ({exc}).",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        # Absolute mode. --anchor only resolves a --last window's end, so
        # supplying it here is a misuse: reject rather than silently ignore.
        if args.anchor is not None:
            print(
                "Error: --anchor is only valid with --last; "
                "it has no meaning for an explicit --start/--end window.",
                file=sys.stderr,
            )
            sys.exit(2)
        # Validate the explicit window before any network/login: malformed ISO
        # dates and reversed ranges fail fast here rather than after login.
        try:
            start_check = date.fromisoformat(args.start)
        except ValueError as exc:
            print(
                f"Error: invalid --start value {args.start!r}: "
                f"expected an ISO date YYYY-MM-DD ({exc}).",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            end_check = date.fromisoformat(args.end)
        except ValueError as exc:
            print(
                f"Error: invalid --end value {args.end!r}: "
                f"expected an ISO date YYYY-MM-DD ({exc}).",
                file=sys.stderr,
            )
            sys.exit(2)
        if start_check > end_check:
            print(
                f"Error: --start {args.start} is after --end {args.end}; the range is reversed.",
                file=sys.stderr,
            )
            sys.exit(2)

    shortname = SHORTNAMES[args.version]
    out = Path(args.output)

    if args.last is not None:
        # Discovery needs login. Run it first, then echo the resolved window
        # to stderr before any download.
        earthaccess.login()
        try:
            end_date = _discover_end(shortname, anchor_date)
        except GranuleShapeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        # The window spans n_days calendar days inclusive, so
        # start = end - (n_days - 1).
        start_date = end_date - timedelta(days=n_days - 1)
        start = start_date.isoformat()
        end = end_date.isoformat()
        print(
            f"resolved --last {args.last} (anchor={anchor_date.isoformat()}) -> "
            f"{start}..{end} ({n_days} days)",
            file=sys.stderr,
        )
    else:
        # Absolute mode. Normalize --start/--end through date.fromisoformat so
        # the downstream search, time-slice, and cache key all use the validated
        # date (its isoformat), not the raw input string. date.fromisoformat on
        # 3.12 also accepts compact (20260501) and ISO-week (2026-W18-1) forms;
        # normalizing here guarantees validated == queried. For an already-
        # YYYY-MM-DD input this is byte-identical (isoformat of a parsed
        # YYYY-MM-DD date is the same string). The earlier validation block
        # already proved these parse and are not reversed.
        start = date.fromisoformat(args.start).isoformat()
        end = date.fromisoformat(args.end).isoformat()
        # start_date/end_date are only consumed by the relative-mode short-window
        # warning below; absolute mode has no requested day count.
        start_date = end_date = None

    # Cache/provenance args record the resolved concrete window, never the
    # relative --last/--anchor inputs, so the same resolved window cache-hits
    # and a relative spec never false-hits across days. Mirrors chirps-fetch's
    # explicit-args cache key. The cache CHECK uses the requested resolved
    # window; in relative mode a short window re-stamps a shorter effective end
    # (see below), so a later request for the full window misses and re-fetches.
    requested_entry = {
        "skill": "imerg-fetch",
        "version": _RHIZA_SKILL_VERSION,
        "args": {"start": start, "end": end, "version": args.version},
        "input": None,
    }
    if _cache_hit(out, requested_entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    print(
        f"Fetching IMERG {args.version} ({shortname}) {start} -> {end}",
        file=sys.stderr,
    )

    # Fetch over the EXACT resolved [start, end]. In relative mode this is a
    # second search, distinct from end-discovery: discovery only finds the
    # latest granule, while this search must cover the whole resolved window
    # regardless of how far end sits behind the anchor. In absolute mode it is
    # the only search. Relative mode already logged in for discovery; absolute
    # mode logs in here.
    if args.last is None:
        earthaccess.login()
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(start, end),
    )
    if not results:
        raise RuntimeError(f"No IMERG {args.version} granules found in {start}..{end}")
    print(f"Found {len(results)} granules", file=sys.stderr)

    # The stamp records the cache key written into rhiza_history. It defaults to
    # the requested resolved window; relative mode refines it to the effective
    # end (last day actually present) when the window is short, so a later
    # request for the full window misses the cache and re-fetches days that have
    # since published. Absolute mode is unchanged.
    stamp_entry = requested_entry

    # If the fetched window covers fewer than the requested number of distinct
    # days (a genuine data gap or near the dataset start), warn with the
    # effective covered span rather than silently presenting a short series as
    # complete. Accept what exists. Only meaningful for relative mode, where
    # n_days is the requested length; absolute mode has no requested day count.
    if args.last is not None:
        # Distinct granule calendar dates within [start_date, end_date], used
        # both to decide whether the fetched window covers the full requested
        # span and to derive the effective end (last present day).
        try:
            present_days = {d for r in results if start_date <= (d := _granule_date(r)) <= end_date}
        except GranuleShapeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        covered_days = len(present_days)
        if covered_days < n_days:
            # No granule day falls inside [start, end] at all, even though
            # search_data returned overlap granules just outside the bounds.
            # There is nothing in-window to write.
            if not present_days:
                print(
                    f"Error: no granule day falls within "
                    f"{start_date.isoformat()}..{end_date.isoformat()}",
                    file=sys.stderr,
                )
                sys.exit(2)
            # Stamp the EFFECTIVE end (last present day) rather than the
            # requested end, mirroring chirps-fetch, so the short zarr is not
            # cached as a complete window.
            effective_end_iso = max(present_days).isoformat()
            stamp_entry = {
                **requested_entry,
                "args": {"start": start, "end": effective_end_iso, "version": args.version},
            }
            print(
                f"WARNING: requested {n_days} days ({start}..{end}) but only "
                f"{covered_days} distinct day(s) are available in that span; "
                f"writing the available days through {effective_end_iso} "
                "(genuine data gap or near the dataset start). Caching the "
                "effective window so a later request for the full window "
                "re-fetches the missing tail.",
                file=sys.stderr,
            )

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
            rhiza_history=json.dumps([stamp_entry], sort_keys=True),
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
