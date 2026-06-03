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

# How far back from today the `latest`-granule discovery search looks. This is
# independent of the requested window length: it only needs to span the
# product's realistic production lag (IMERG late runs a few days behind; final
# can run months behind), not the window. The actual window is fetched by a
# separate search over the exact resolved [start, end] (see main()), so a wide
# discovery lookback never inflates the fetched data.
_DISCOVERY_LOOKBACK_DAYS = 200

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

# Upper bound on a relative offset's resolved day count. IMERG begins in year
# 2000, so an offset cannot meaningfully span more than ~26 years; 36525 days
# (~100 years) is far beyond any real window yet small enough that the date
# arithmetic cannot raise OverflowError. Rejecting above this cap keeps the
# failure pre-network.
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


def _discover_latest(shortname: str) -> date:
    """Find the newest available granule on or before today (UTC) and return its
    date — the `latest` resolver for IMERG.

    Searches a lookback window ending today whose width is set by the product's
    realistic production lag (``_DISCOVERY_LOOKBACK_DAYS``), not by the requested
    window length. The resolved window is fetched separately over its exact
    ``[start, end]`` (see ``main()``), so this discovery search only has to
    surface the most-recent granule. Returns the max granule date ``<= today``.
    Exits 2 if no granule falls in that lookback. Requires earthaccess.login()
    to have been called.
    """
    today = datetime.now(UTC).date()
    lookback_start = today - timedelta(days=_DISCOVERY_LOOKBACK_DAYS)
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(lookback_start.isoformat(), today.isoformat()),
    )
    try:
        on_or_before = [r for r in results if _granule_date(r) <= today]
    except GranuleShapeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    if not on_or_before:
        print(
            f"Error: no IMERG {shortname} granules available in lookback window "
            f"{lookback_start.isoformat()}..{today.isoformat()}; cannot resolve "
            "'latest'.",
            file=sys.stderr,
        )
        sys.exit(2)
    return max(_granule_date(r) for r in on_or_before)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
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
    p.add_argument("--version", default="late", choices=list(SHORTNAMES))
    args = p.parse_args()

    shortname = SHORTNAMES[args.version]
    out = Path(args.output)

    # `latest` discovery needs an earthaccess.login() before the CMR search.
    # Memoize so a window referencing `latest` at both ends discovers once. The
    # closure logs in lazily on first use, so an all-absolute or now-only window
    # performs no discovery login (absolute behavior is unchanged).
    _latest_cache = {}

    def _latest() -> date:
        if "v" not in _latest_cache:
            earthaccess.login()
            _latest_cache["v"] = _discover_latest(shortname)
        return _latest_cache["v"]

    # Resolve --start/--end to concrete inclusive dates. Malformed tokens and
    # post-resolution reversed ranges exit 2 before any network call (a `latest`
    # token does trigger discovery, but that is the resolution itself, not a
    # data fetch). Absolute YYYY-MM-DD endpoints normalize through
    # date.fromisoformat, so for an already-YYYY-MM-DD input the resolved
    # isoformat is byte-identical to the raw input.
    start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
    start = start_date.isoformat()
    end = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    # Cache/provenance args record the resolved concrete window, never the
    # relative token, so the same resolved window cache-hits and a relative spec
    # never false-hits across days. Mirrors chirps-fetch's explicit-args cache key.
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

    # Fetch over the EXACT resolved [start, end]. If a `latest` token already
    # logged in during discovery, login() is idempotent; otherwise this is the
    # first and only login.
    earthaccess.login()
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(start, end),
    )
    if not results:
        raise RuntimeError(f"No IMERG {args.version} granules found in {start}..{end}")
    print(f"Found {len(results)} granules", file=sys.stderr)

    requested_span = (end_date - start_date).days + 1

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

        # Short-window protection (mirrors chirps-fetch's tail-vs-mid-gap
        # handling). IMERG late runs a few days behind realtime, so a window
        # whose end is at or near today (e.g. `--end now`) can resolve to a span
        # whose trailing days are not yet published. The present-day set is
        # derived from the WRITTEN dataset's own time axis (after the
        # sel(time=slice) trim above), NOT from CMR BeginningDateTime metadata,
        # so the cache stamp matches the data actually written.
        import numpy as np

        # np.datetime64(t, "D") truncates each ns timestamp to its calendar day;
        # .item() on a datetime64[D] yields a datetime.date.
        present_days = sorted({np.datetime64(t, "D").item() for t in ds["time"].values})
        # ds.sel already restricts to [start, end]; defensively re-bound in case
        # any boundary granule slipped through the slice.
        present_days = [d for d in present_days if start_date <= d <= end_date]
        covered_days = len(present_days)
        stamp_entry = requested_entry
        if covered_days == 0:
            # The granules search returned overlap granules just outside the
            # bounds, but no granule day falls inside [start, end]: nothing
            # in-window to write.
            print(
                f"Error: no granule day falls within {start}..{end}",
                file=sys.stderr,
            )
            sys.exit(2)
        if covered_days < requested_span:
            last_present = present_days[-1]
            expected_tail = [
                start_date + timedelta(days=i)
                for i in range(requested_span)
                if start_date + timedelta(days=i) > last_present
            ]
            missing_days = sorted(
                set(start_date + timedelta(days=i) for i in range(requested_span))
                - set(present_days)
            )
            if missing_days != expected_tail:
                # Interior hole: a missing day precedes a later present day. This
                # is a server/data gap, not realtime lag; refuse to silently cache
                # a window with a hole in the middle (mirrors chirps).
                print(
                    f"Error: non-trailing missing day(s) "
                    f"{', '.join(d.isoformat() for d in missing_days)} within "
                    f"{start}..{end} — server/data gap, not lag. Refusing to write "
                    "a partial zarr with a hole in the middle.",
                    file=sys.stderr,
                )
                sys.exit(2)
            # Contiguous trailing gap: warn and stamp the EFFECTIVE end (last day
            # actually present) so a later run re-fetches the now-published tail
            # instead of short-circuiting on a cache hit against a partial window.
            effective_end_iso = last_present.isoformat()
            stamp_entry = {
                **requested_entry,
                "args": {"start": start, "end": effective_end_iso, "version": args.version},
            }
            print(
                f"WARNING: requested {requested_span} days ({start}..{end}) but only "
                f"{covered_days} distinct day(s) are present in that span; writing the "
                f"available days through {effective_end_iso} (trailing days not yet "
                "published, or near the dataset start). Caching the effective window "
                "so a later request for the full window re-fetches the missing tail.",
                file=sys.stderr,
            )

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
