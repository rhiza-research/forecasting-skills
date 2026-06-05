# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "earthaccess",
#   "h5py",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch SMAP SPL3SMP_E soil moisture via Earthdata and write a Rhiza Envelope Zarr."""

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import earthaccess
import numpy as np

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.0"

_SHORT_NAME = "SPL3SMP_E"
_FILL = -9999.0
# How far back the `latest` search looks for the newest published granule. SMAP
# L3 runs a few days behind realtime; 30 days covers the lag plus short gaps.
_LATEST_LOOKBACK_DAYS = 30
# Parses the YYYYMMDD acquisition date out of a granule filename, e.g.
# SMAP_L3_SM_P_E_20240601_R19240_001.h5.
_GRANULE_DATE_RE = re.compile(r"_(\d{8})_")

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


def _bbox_subset(ds, bbox: str):
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox.

    The slice direction follows each axis's own monotonic order (SMAP latitude is
    descending; longitude is ascending in [-180, 180)), so the same bbox works
    regardless of axis order.
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


def _granule_date(granule) -> date:
    """Parse the acquisition date from a granule's data-file name."""
    for url in granule.data_links():
        m = _GRANULE_DATE_RE.search(url.rsplit("/", 1)[-1])
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d").date()
    raise ValueError("could not parse a YYYYMMDD date from the granule file name")


def _slice_from_file(path: str, group: str):
    """Read one SPL3SMP_E granule into a (latitude, longitude) DataArray.

    The EASE-Grid 2.0 global latitude/longitude are stored as 2-D arrays but are
    constant along one axis each, so they reduce to 1-D coordinate vectors.
    """
    import h5py
    import xarray as xr

    with h5py.File(path, "r") as h:
        grp = h[f"Soil_Moisture_Retrieval_Data_{group}"]
        sm = grp["soil_moisture"][:].astype("float64")
        lat2d = grp["latitude"][:].astype("float64")
        lon2d = grp["longitude"][:].astype("float64")
        units = grp["soil_moisture"].attrs.get("units", b"cm3/cm3")
    units = units.decode() if isinstance(units, bytes) else str(units)

    sm = np.where(sm == _FILL, np.nan, sm)
    lat2d = np.where(lat2d == _FILL, np.nan, lat2d)
    lon2d = np.where(lon2d == _FILL, np.nan, lon2d)
    # Reduce the degenerate 2-D geolocation to 1-D: each row has one latitude and
    # each column one longitude (verified row/col-constant on the global grid).
    lat1d = np.nanmean(lat2d, axis=1)
    lon1d = np.nanmean(lon2d, axis=0)

    da = xr.DataArray(
        sm,
        dims=("latitude", "longitude"),
        coords={"latitude": lat1d, "longitude": lon1d},
        name="soil_moisture",
    )
    da.attrs["units"] = "cm3/cm3" if units in ("cm**3/cm**3", "cm3/cm3") else units
    da.attrs["long_name"] = "volumetric soil moisture"
    return da


def _discover_latest(lookback_days: int) -> date:
    """`latest` resolver: newest SPL3SMP_E granule date on or before today."""
    today = datetime.now(UTC).date()
    lookback_start = today - timedelta(days=lookback_days)
    results = earthaccess.search_data(
        short_name=_SHORT_NAME,
        temporal=(lookback_start.isoformat(), today.isoformat()),
    )
    dates = [d for d in (_granule_date(r) for r in results) if d <= today]
    if not dates:
        print(
            f"Error: no SMAP {_SHORT_NAME} granules in lookback window "
            f"{lookback_start.isoformat()}..{today.isoformat()}; cannot resolve 'latest'.",
            file=sys.stderr,
        )
        sys.exit(2)
    return max(dates)


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
        "--end", required=True, help="End date (inclusive). Same date grammar as --start."
    )
    p.add_argument("--bbox", help="Spatial subset N/W/S/E decimal degrees. Omit for the full grid.")
    p.add_argument(
        "--overpass",
        choices=["AM", "PM"],
        default="AM",
        help="Half-orbit overpass group to read (AM = 6am descending, PM = 6pm ascending). Default AM.",
    )
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    import xarray as xr

    out = Path(args.output)

    # `latest` discovery needs an earthaccess.login() before the CMR search.
    # Memoized so a window referencing `latest` at both ends discovers once; an
    # all-absolute or now-only window performs no discovery login.
    _latest_cache: dict = {}

    def _latest() -> date:
        if "v" not in _latest_cache:
            earthaccess.login()
            _latest_cache["v"] = _discover_latest(_LATEST_LOOKBACK_DAYS)
        return _latest_cache["v"]

    start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    entry = {
        "skill": "smap-fetch",
        "version": _RHIZA_SKILL_VERSION,
        "args": {
            "bbox": args.bbox,
            "overpass": args.overpass,
            "start": start_iso,
            "end": end_iso,
        },
        "input": None,
    }
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    print(f"Fetching SMAP {_SHORT_NAME} ({args.overpass}) {start_iso}..{end_iso}", file=sys.stderr)
    earthaccess.login()
    results = earthaccess.search_data(
        short_name=_SHORT_NAME,
        temporal=(start_iso, end_iso),
    )
    # CMR's temporal filter is overlap-based; keep only granules whose own date is
    # inside the requested window, and one per day.
    in_window = {}
    for r in results:
        d = _granule_date(r)
        if start_date <= d <= end_date:
            in_window.setdefault(d, r)
    if not in_window:
        print(f"Error: no SMAP granule day within {start_iso}..{end_iso}.", file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(in_window)} granule day(s)", file=sys.stderr)

    slices = []
    times = []
    with tempfile.TemporaryDirectory(prefix="smap-fetch-") as td:
        for d in sorted(in_window):
            files = earthaccess.download([in_window[d]], local_path=td)
            da = _slice_from_file(files[0], args.overpass)
            if args.bbox:
                da = _bbox_subset(da.to_dataset(name="soil_moisture"), args.bbox)["soil_moisture"]
            slices.append(da)
            times.append(np.datetime64(d.isoformat()))

    ds = xr.concat(slices, dim="time").assign_coords(time=("time", times)).to_dataset()
    ds.attrs.update(
        rhiza_source="smap",
        rhiza_history=json.dumps([entry], sort_keys=True),
    )
    _stamp_cf_attrs(ds)
    for v in ds.variables:
        ds[v].encoding = {}

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({dict(ds.sizes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
