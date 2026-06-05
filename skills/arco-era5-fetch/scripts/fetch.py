# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "xarray",
#   "zarr",
#   "gcsfs",
#   "numpy",
# ]
# ///
"""Fetch ARCO-ERA5 reanalysis from the public Google Cloud Zarr and write a Rhiza Envelope Zarr."""

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"

# Public, credential-free ARCO-ERA5 analysis-ready store: 0.25 deg equiangular
# lat/lon, hourly, dims (time, latitude, longitude, level). Opened anonymously.
# Path is the published value from the arco-era5 README, not a guess.
_ARCO_STORE = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
_STORAGE_OPTIONS = {"token": "anon"}

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

    `now` is the current UTC date. `latest_fn` is a zero-arg callable that
    discovers the newest available date; it is invoked at most once per process
    (the caller memoizes) and only when a token references `latest`.
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


def _np_to_date(value) -> date:
    """Convert a numpy datetime64 to a calendar date (truncating any time-of-day)."""
    return date.fromisoformat(np.datetime_as_string(value, unit="D"))


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


def _stamp_cf_attrs(ds):
    """Stamp CF standard_name/units/axis on spatial + time coords (non-destructive)."""
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


def _normalize_longitude(ds):
    """Map ERA5's native 0..360 longitude onto [-180, 180) and sort ascending.

    ERA5 stores longitude as 0..359.75. Normalizing lets an N/W/S/E bbox with
    negative west/east values (the convention the other fetchers and resolve-region
    use) select the right cells.
    """
    lon = ((ds["longitude"] + 180) % 360) - 180
    ds = ds.assign_coords(longitude=lon)
    return ds.sortby("longitude")


def _bbox_subset(ds, bbox: str):
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox.

    The slice direction follows each axis's own monotonic order (ERA5 latitude is
    descending; longitude is ascending after normalization), so the same bbox
    works regardless of axis order.
    """
    try:
        north, west, south, east = (float(x) for x in bbox.split("/"))
    except ValueError:
        print("Error: --bbox must be four decimal degrees N/W/S/E.", file=sys.stderr)
        sys.exit(2)
    lat = ds["latitude"].values
    lon = ds["longitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    lon_slice = slice(west, east) if lon[0] < lon[-1] else slice(east, west)
    ds = ds.sel(latitude=lat_slice, longitude=lon_slice)
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        print(
            f"Error: --bbox {bbox} selects no grid cells; check the extent and N/W/S/E order.",
            file=sys.stderr,
        )
        sys.exit(1)
    return ds


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--start",
        required=True,
        help=(
            "Range start, inclusive. Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days)."
        ),
    )
    p.add_argument(
        "--end",
        required=True,
        help="Range end, inclusive. Same date grammar as --start.",
    )
    p.add_argument("--bbox", help="Spatial subset N/W/S/E decimal degrees. Omit for the full grid.")
    p.add_argument(
        "--variable",
        "-v",
        action="append",
        help="Restrict to this data variable. Repeat once per variable; omit for all (large).",
    )
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    import xarray as xr

    # Lazy open with chunks=None: reads only metadata, so `latest` resolution and
    # the cache check run before any array bytes are pulled, and xarray's lazy
    # indexing prunes to the bbox/time/variable selection before reading — so only
    # that selection is materialized. (A dask-backed open forces the longitude
    # normalization sort to pull the whole global grid per step, which is far slower
    # for a bbox request with no memory benefit; the selection itself is the bound.)
    ds = xr.open_zarr(_ARCO_STORE, storage_options=_STORAGE_OPTIONS, chunks=None)

    # `latest` resolves from the store's own time coord max; memoized so it is
    # read at most once and only when a token references `latest`.
    _latest_cache: dict = {}

    def _latest() -> date:
        if "v" not in _latest_cache:
            _latest_cache["v"] = _np_to_date(ds["time"].values.max())
        return _latest_cache["v"]

    start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    # Cache key: the argparse namespace minus the output path, with the resolved
    # concrete dates substituted for any relative token.
    args_dict = {k: v for k, v in vars(args).items() if k != "output"}
    args_dict["start"] = start_iso
    args_dict["end"] = end_iso
    entry = {
        "skill": "arco-era5-fetch",
        "version": _RHIZA_SKILL_VERSION,
        "args": args_dict,
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    ds = _normalize_longitude(ds)
    if args.bbox:
        ds = _bbox_subset(ds, args.bbox)

    # Inclusive whole-day window over the hourly time axis.
    ds = ds.sel(time=slice(np.datetime64(f"{start_iso}T00:00"), np.datetime64(f"{end_iso}T23:59")))
    if ds.sizes.get("time", 0) == 0:
        print(f"Error: ARCO-ERA5 has no data in {start_iso}..{end_iso}.", file=sys.stderr)
        sys.exit(1)

    if args.variable:
        missing = [v for v in args.variable if v not in ds.data_vars]
        if missing:
            print(
                f"Error: variable(s) not in ARCO-ERA5: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(ds.data_vars))}",
                file=sys.stderr,
            )
            sys.exit(2)
        ds = ds[args.variable]
    else:
        print(
            "Note: no --variable given; fetching all data variables (large). Pass -v to restrict.",
            file=sys.stderr,
        )

    print(f"Fetching arco-era5 {start_iso}..{end_iso}", file=sys.stderr)

    ds.attrs.update(
        rhiza_source="arco-era5",
        rhiza_history=json.dumps([entry], sort_keys=True),
    )
    _stamp_cf_attrs(ds)
    # Per-variable encoding is not part of the envelope contract; clear it so the
    # output is written with this skill's own codecs.
    for v in ds.variables:
        ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(out, mode="w", consolidated=True)

    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
