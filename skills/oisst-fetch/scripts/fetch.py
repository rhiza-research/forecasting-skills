# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
#   "netCDF4",
#   "cf_xarray",
#   "cf_units",
#   "cftime",
# ]
# ///
"""Fetch NOAA OISST v2.1 daily sea-surface temperature from NOAA PSL OPeNDAP and write a Rhiza Envelope Zarr."""

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import cf_units
import cf_xarray  # noqa: F401 -- registers the .cf accessor used in the write-side decode check
import numpy as np
import xarray as xr

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.1"

# --- Source -> output transforms ---
#
# Divergences this skill applies to the raw NOAA OISST v2.1 source. Everything not
# listed here passes through unchanged (the unstated default).
#
# - VARIABLE RENAMES: the source dimension/coord `lat` is renamed to `latitude`
#   and `lon` to `longitude` (`.rename({"lat": "latitude", "lon": "longitude"})`).
# - UNITS: `sst` units PASS THROUGH VERBATIM. The source value (`degC`) is
#   forwarded unchanged; it is only validated as a udunits temperature unit
#   convertible to K before the standard_name is stamped — no remap, no conversion.
# - standard_name ASSIGNMENT: the source `sst` carries long_name + units but no
#   standard_name; this skill stamps `standard_name=sea_surface_temperature`, a
#   valid CF standard name (CF standard name table; canonical units K, to which the
#   source `degC` is udunits-convertible), after the units validation above.
# - LONGITUDE NORMALIZATION: the source's native 0..360 longitude axis is mapped
#   onto [-180, 180) and sorted ascending.
# - STALE/DANGLING ATTR STRIPPING: source attrs that describe the pre-subset
#   global/per-year extent or contradict the written data are removed —
#   `actual_range`, `valid_range`, `_ChunkSizes`, `missing_value`, `valid_min`,
#   `valid_max` — and a dangling `bounds` attr is dropped when its bounds variable
#   was not carried over.

# Public, credential-free NOAA PSL OPeNDAP server. One file per year holds daily
# 0.25-degree global SST (variable `sst`, degC) on dims (time, lat, lon), lon
# 0..360. OPeNDAP lets us subset a bbox/time window without downloading the whole
# yearly file.
_OPENDAP_URL = (
    "https://psl.noaa.gov/thredds/dodsC/Datasets/noaa.oisst.v2.highres/sst.day.mean.{year}.nc"
)

# CF-1.13 global attribute block stamped on the output. The source file declares
# CF-1.5 and carries NOAA/NCEI provenance; we assert the higher conventions
# version we validate against and record the NOAA OISST v2.1 / NOAA PSL OPeNDAP
# lineage. References point at the PSL dataset page and the v2.1 method paper.
_CF_GLOBAL_ATTRS = {
    "Conventions": "CF-1.13",
    "title": (
        "NOAA/NCEI 1/4 Degree Daily Optimum Interpolation Sea Surface Temperature "
        "(OISST) Analysis, Version 2.1"
    ),
    "source": (
        "NOAA OISST v2.1 daily analysis, read from NOAA PSL OPeNDAP (sst.day.mean.<year>.nc)"
    ),
    "institution": "NOAA/National Centers for Environmental Information",
    "references": (
        "https://www.psl.noaa.gov/data/gridded/data.noaa.oisst.v2.highres.html ; "
        "Huang et al. 2021, https://doi.org/10.1175/JCLI-D-20-0166.1"
    ),
    "history": (
        "oisst-fetch: subset NOAA OISST v2.1 daily SST from NOAA PSL OPeNDAP "
        "to the resolved time window and bounding box"
    ),
}

# CF standard name and canonical method for the SST variable. The source `sst`
# carries long_name + units=degC but no standard_name; `sea_surface_temperature`
# is a valid CF standard name (canonical units K; degC is udunits-convertible to
# it), so we assert it on write after validating the source units.
_SST_STANDARD_NAME = "sea_surface_temperature"
_SST_LONG_NAME = "Daily Sea Surface Temperature"

# Source attrs computed over the full per-year/global extent that no longer
# describe the bbox/time subset we write. `actual_range`/`valid_range` are the
# source's own min/max over the global grid (the longitude `actual_range` even
# still reads on the un-normalized 0..360 axis), and `_ChunkSizes` reflects the
# source file's HDF5 layout, not our output. `missing_value`/`valid_min`/
# `valid_max` are source masking attrs (the OISST land sentinel ~ -9.96921e36, and
# the valid bounds) that would contradict the NaN `_FillValue` we stamp in
# encoding — that NaN is the single source of truth for missing. Drop them all so
# no attr contradicts the written data.
_STALE_RANGE_ATTRS = (
    "actual_range",
    "valid_range",
    "_ChunkSizes",
    "missing_value",
    "valid_min",
    "valid_max",
)

# Write-side time encoding. udunits time-reference + an explicit calendar, set in
# the encoding (not left to xarray defaults) so the output's time axis decodes
# deterministically.
_TIME_UNITS = "days since 1970-01-01 00:00:00"
_TIME_CALENDAR = "standard"

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


def _np_to_date(value) -> date:
    """Convert a numpy datetime64 to a calendar date (truncating any time-of-day)."""
    return date.fromisoformat(np.datetime_as_string(value, unit="D"))


def _load_history(zarr_path: Path) -> list:
    try:
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


def _store_is_complete(out: Path) -> bool:
    """Cheaply verify a candidate cache-hit store is readable and structurally sound.

    A mid-run failure can leave a partial Zarr whose root attrs (and so its
    rhiza_history) were written before a later year's data transfer failed. The
    write path removes such a store on failure, so this is the backstop for stores
    left partial by other means (a killed process, a full disk mid-append):

    - `sst` must exist and be non-empty;
    - read the LAST time step's corner element of `sst` (the slice an interrupted
      append would be missing) to confirm the array decodes;
    - the `time` coord must be present, fully valued (no NaT from a half-written
      append), and strictly increasing.

    Any failure is treated as an incomplete store and a cache miss.
    """
    try:
        with xr.open_zarr(out, consolidated=True) as ds:
            if "sst" not in ds.data_vars or ds["sst"].size == 0:
                return False
            if "time" not in ds.coords or ds.sizes.get("time", 0) == 0:
                return False
            corner = {d: (-1 if d == "time" else 0) for d in ds["sst"].dims}
            ds["sst"].isel(corner).load()
            time_vals = ds["time"].values
            if np.isnat(time_vals).any():
                return False
            if time_vals.size > 1 and not np.all(np.diff(time_vals) > np.timedelta64(0)):
                return False
    except Exception:  # noqa: BLE001 -- any read failure means the store is not a usable cache hit
        return False
    return True


def _cache_hit(out: Path, entry: dict) -> bool:
    """Return True if the zarr at `out` was produced by this same entry AND is complete."""
    if not out.exists():
        return False
    history = _load_history(out)
    if not history:
        return False
    existing_entry = history[0]
    if not (
        existing_entry.get("skill") == entry["skill"]
        and existing_entry.get("version") == entry["version"]
        and existing_entry.get("args") == entry["args"]
        and existing_entry.get("input") == entry["input"]
    ):
        return False
    # The history entry can be present on a partial store written before a later
    # year failed; verify the data actually reads back before honoring the hit.
    return _store_is_complete(out)


def _strip_dangling_bounds(ds):
    """Remove a `bounds` attr from any coord when the named bounds variable is absent.

    Selecting `[["sst"]]` drops a `time_bnds`/`lat_bnds`/`lon_bnds` variable while
    a `bounds` attr stamped on the parent coord can survive, leaving a CF
    dangling reference. Strip any such orphaned `bounds` attr.
    """
    present = set(ds.variables)
    for name in ds.coords:
        bnds = ds[name].attrs.get("bounds")
        if bnds is not None and bnds not in present:
            del ds[name].attrs["bounds"]
    return ds


def _stamp_cf(ds):
    """Stamp full CF-1.13 attrs: global block, coord standard_name/units/axis, sst attrs.

    The source `sst` units (`degC`) are validated with a real udunits check before
    we assert the CF standard_name; an invalid/unconvertible unit halts rather
    than emit a false CF claim. lat/lon are stamped after the rename to
    latitude/longitude; time gets standard_name/axis (its units/calendar are set
    in the write encoding, not here).
    """
    ds.attrs.update(_CF_GLOBAL_ATTRS)

    if "latitude" in ds.coords:
        ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
        ds["latitude"].attrs.setdefault("long_name", "Latitude")
    if "longitude" in ds.coords:
        ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
        ds["longitude"].attrs.setdefault("long_name", "Longitude")
    if "time" in ds.coords:
        ds["time"].attrs.update(standard_name="time", axis="T")
        ds["time"].attrs.setdefault("long_name", "Time")

    src_units = ds["sst"].attrs.get("units")
    try:
        unit = cf_units.Unit(src_units)
        valid = (not unit.is_no_unit()) and unit.is_convertible(cf_units.Unit("K"))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        print(
            f"Error: source OISST sst units {src_units!r} are not a udunits temperature "
            "unit convertible to K; refusing to stamp CF "
            f"standard_name={_SST_STANDARD_NAME!r} under an invalid units claim. The "
            "source product changed its units; this fetcher must be revisited.",
            file=sys.stderr,
        )
        sys.exit(1)
    ds["sst"].attrs["standard_name"] = _SST_STANDARD_NAME
    ds["sst"].attrs["long_name"] = _SST_LONG_NAME
    ds["sst"].attrs["units"] = src_units

    # Drop source range/chunk bookkeeping that describes the pre-subset extent.
    for name in ("sst", "latitude", "longitude", "time"):
        if name in ds.variables:
            for attr in _STALE_RANGE_ATTRS:
                ds[name].attrs.pop(attr, None)

    _strip_dangling_bounds(ds)
    return ds


def _cf_decode_check(out: Path) -> None:
    """Reopen the written store and confirm cf-xarray resolves the X/Y/T axes.

    A write that does not decode as CF (coord attrs missing/wrong, axes
    unresolvable) is a defect under the full-CF contract; surface it rather than
    ship a store that only looks compliant.
    """
    try:
        with xr.open_zarr(out, consolidated=True, decode_cf=True) as ds:
            axes = ds.cf.axes
            missing = [ax for ax in ("X", "Y", "T") if ax not in axes]
    except Exception as exc:  # noqa: BLE001 -- a failed decode is itself the defect to report
        print(
            f"Error: wrote {out} but cf-xarray could not decode it ({exc}); the output "
            "is not CF-compliant.",
            file=sys.stderr,
        )
        sys.exit(1)
    if missing:
        print(
            f"Error: wrote {out} but cf-xarray did not resolve axes {missing} "
            "(expected X/Y/T); the output is not CF-compliant.",
            file=sys.stderr,
        )
        sys.exit(1)


def _normalize_longitude(ds):
    """Map OISST's native 0..360 longitude onto [-180, 180) and sort ascending, so
    an N/W/S/E bbox with negative west/east values selects correctly."""
    lon = ((ds["longitude"] + 180) % 360) - 180
    ds = ds.assign_coords(longitude=lon)
    return ds.sortby("longitude")


def _bbox_subset(ds, north, west, south, east):
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox.

    The slice direction follows each axis's own monotonic order (OISST latitude is
    ascending; longitude is ascending after normalization), so the same bbox works
    regardless of axis order. A west>east bbox crosses the antimeridian; on a
    [-180, 180) grid that is the complement of the [east, west] interior, so drop
    the interior and keep the two outer wings, consistent with sibling skills.
    """
    lat = ds["latitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    if west <= east:
        ds = ds.sel(latitude=lat_slice, longitude=slice(west, east))
    else:
        # Antimeridian-crossing bbox: keep [-180, east] and [west, 180), i.e. drop
        # the interior (east, west) band.
        ds = ds.sel(latitude=lat_slice)
        ds = ds.where((ds["longitude"] <= east) | (ds["longitude"] >= west), drop=True)
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        print(
            "Error: --bbox selects no grid cells; check the extent and N/W/S/E order.",
            file=sys.stderr,
        )
        sys.exit(1)
    return ds


def _parse_bbox(bbox: str) -> tuple:
    try:
        north, west, south, east = (float(x) for x in bbox.split("/"))
    except ValueError:
        print("Error: --bbox must be four decimal degrees N/W/S/E.", file=sys.stderr)
        sys.exit(2)
    return north, west, south, east


def _open_year(year: int):
    """Open one OISST year's OPeNDAP dataset.

    No dask chunking: the netCDF4 OPeNDAP backend's own lazy indexing reads only
    the cells touched by the later .sel()/.load(), and a chunked (dask) read over
    this backend was observed to silently write zeros instead of the real values.
    """
    return xr.open_dataset(_OPENDAP_URL.format(year=year))


def _is_availability_failure(exc: Exception) -> bool:
    """Heuristic: does this error mean the requested year file is absent (outside
    the served range / not yet published) rather than a transport problem? The
    netCDF4 backend prefixes essentially every error with `NetCDF:`, so a bare
    `netcdf` marker cannot separate absence from transport; key absence on the
    specific not-found phrasings instead."""
    text = str(exc).lower()
    markers = ("not found", "no such file", "404", "does not exist", "file not found")
    return any(m in text for m in markers)


def _is_transport_failure(exc: Exception) -> bool:
    """Heuristic: does this look like an OPeNDAP transport/size failure rather than
    a code bug or a missing-file (availability) error? PSL's OPeNDAP server raises a
    DAP error when a request exceeds its size or time limits, and connection/timeout
    errors look similar. Availability (not-found) errors are explicitly excluded so a
    genuine absent-year file is not misreported as oversized — the bare `netcdf`
    marker is omitted because the netCDF4 backend prefixes every error with `NetCDF:`,
    which would otherwise swallow availability failures."""
    if _is_availability_failure(exc):
        return False
    text = str(exc).lower()
    markers = ("dap failure", "dap2", "dap", "curl", "connection", "timed out", "timeout")
    return any(m in text for m in markers)


def _remove_store(out: Path) -> None:
    """Remove a candidate output path, whether it is a Zarr directory or a file."""
    if out.is_dir():
        shutil.rmtree(out)
    elif out.exists():
        out.unlink()


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_RHIZA_SKILL_VERSION}",
    )
    p.add_argument(
        "--start",
        required=True,
        help=(
            "Range start, inclusive. Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days)."
        ),
    )
    p.add_argument(
        "--end", required=True, help="Range end, inclusive. Same date grammar as --start."
    )
    p.add_argument("--bbox", help="Spatial subset N/W/S/E decimal degrees. Omit for the full grid.")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    def _latest() -> date:
        # Newest available day lives in the current-year file; fall back to the
        # previous year early in January before the new year's file appears. Each
        # open is classified: a transport failure (server unreachable) is distinct
        # from a genuine absence of the year file.
        today = datetime.now(UTC).date()
        transport_err = None
        for year in (today.year, today.year - 1):
            try:
                with _open_year(year) as dy:
                    return _np_to_date(dy["time"].values.max())
            except Exception as exc:  # noqa: BLE001 -- try the previous year, else classify below
                # Only a true transport marker (not a not-found/availability error)
                # routes to the "server unreachable" message; an absent year file
                # (early January, before the new year's file appears) falls through
                # to the availability guidance below.
                if _is_transport_failure(exc):
                    transport_err = exc
                continue
        if transport_err is not None:
            print(
                "Error: could not resolve 'latest' — NOAA PSL's OPeNDAP server looks "
                f"unreachable (transport failure: {transport_err}). This is not a "
                "data-availability problem; check connectivity and retry.",
                file=sys.stderr,
            )
        else:
            print(
                "Error: could not resolve 'latest' — neither the current nor the "
                f"previous year file ({today.year}, {today.year - 1}) was available on "
                "NOAA PSL OPeNDAP. Use an absolute --start/--end in the served range "
                "(1981-09 to present) instead.",
                file=sys.stderr,
            )
        sys.exit(1)

    start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    entry = {
        "skill": "oisst-fetch",
        "version": _RHIZA_SKILL_VERSION,
        "args": {"bbox": args.bbox, "start": start_iso, "end": end_iso},
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    bbox = _parse_bbox(args.bbox) if args.bbox else None
    time_slice = slice(np.datetime64(f"{start_iso}T00:00"), np.datetime64(f"{end_iso}T23:59"))
    years = list(range(start_date.year, end_date.year + 1))
    print(f"Fetching oisst {start_iso}..{end_iso} (years {years[0]}..{years[-1]})", file=sys.stderr)

    # Stream one year at a time: subset each year to the bbox + window, pull just
    # that slice into memory (eager per-year load avoids the dask-over-OPeNDAP path
    # that silently wrote zeros), then write/append it to Zarr before moving to the
    # next year. Peak resident memory is bounded to a single year's selection rather
    # than the whole multi-year window. rhiza_source/history and CF attrs are
    # stamped on every write because a to_zarr append rewrites the root group attrs
    # from the appended dataset; the entry is identical each time, so the final
    # stamp is stable.
    #
    # `store_created` tracks whether `out` has been written yet. Any mid-loop
    # failure after the first write routes through the cleanup path so a partial
    # store is removed before exit and a later identical run cannot falsely accept
    # it as a cache hit.
    rhiza_history = json.dumps([entry], sort_keys=True)
    store_created = False
    total_time = 0
    for year in years:
        try:
            dy = _open_year(year)
        except Exception as exc:  # noqa: BLE001 -- open is metadata-only; failure = availability/transport
            if store_created:
                _remove_store(out)
            print(
                f"Error: could not open the OISST file for year {year} ({exc}). The year may be "
                "outside the served range (1981-09 to present), or NOAA PSL's OPeNDAP server is "
                "unreachable — check the date range.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            with dy:
                piece = dy[["sst"]].rename({"lat": "latitude", "lon": "longitude"})
                piece = _normalize_longitude(piece)
                # Narrow the TIME axis first, before any spatial selection. The
                # antimeridian branch of _bbox_subset uses an eager `.where(drop=True)`,
                # so doing it before the time subset would materialize the full year's
                # longitude wings across the entire year's time axis; subsetting time
                # first keeps that eager spatial read bounded to the requested window.
                piece = piece.sel(time=time_slice)
                if bbox is not None:
                    piece = _bbox_subset(piece, *bbox)
                piece = piece.load()
        except SystemExit:
            # _bbox_subset already cleaned up its own exit path (no store yet for a
            # first-year bbox miss); propagate.
            raise
        except Exception as exc:  # noqa: BLE001 -- classify the OPeNDAP data-transfer failure
            if store_created:
                _remove_store(out)
            if _is_availability_failure(exc):
                print(
                    f"Error: could not read the OISST file for year {year} ({exc}). The year may be "
                    "outside the served range (1981-09 to present), or that year's file is not yet "
                    "available — check the date range and use an absolute --start/--end in the "
                    "served range.",
                    file=sys.stderr,
                )
            elif _is_transport_failure(exc):
                print(
                    f"Error: OISST OPeNDAP rejected the data transfer for {start_iso}..{end_iso} "
                    f"bbox {args.bbox or 'global'} (year {year}): {exc}. OISST is served over NOAA "
                    "PSL OPeNDAP, which limits request size; this request is too large. Reduce "
                    "--bbox and/or shorten the date range. This is not a credentials or "
                    "data-availability problem — retrying the same request will not help.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Error: unexpected failure reading OISST year {year}: {exc}.", file=sys.stderr
                )
            sys.exit(2)
        if piece.sizes.get("time", 0) == 0:
            continue
        piece.attrs.update(rhiza_source="oisst", rhiza_history=rhiza_history)
        _stamp_cf(piece)
        # Per-variable encoding is not part of the envelope contract; clear it so the
        # output is written with this skill's own codecs, then set the controlled
        # CF write encoding: explicit time units/calendar and an explicit NaN
        # _FillValue for sst land cells.
        for v in piece.variables:
            piece[v].encoding = {}
        piece["time"].encoding["units"] = _TIME_UNITS
        piece["time"].encoding["calendar"] = _TIME_CALENDAR
        piece["sst"].encoding["_FillValue"] = np.float32("nan")
        if not store_created:
            _remove_store(out)
            out.parent.mkdir(parents=True, exist_ok=True)
            piece.to_zarr(out, mode="w", consolidated=True)
            store_created = True
        else:
            piece.to_zarr(out, mode="a", append_dim="time", consolidated=True)
        total_time += piece.sizes["time"]

    if not store_created:
        print(f"Error: OISST has no data in {start_iso}..{end_iso}.", file=sys.stderr)
        sys.exit(1)

    # Final gate: the written store must decode as CF (cf-xarray resolves X/Y/T).
    _cf_decode_check(out)

    print(f"Wrote: {args.output} (time={total_time})", file=sys.stderr)


if __name__ == "__main__":
    main()
