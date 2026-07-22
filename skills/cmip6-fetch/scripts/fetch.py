# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "xarray",
#   "zarr",
#   "gcsfs",
#   "numpy",
#   "pandas",
#   "cftime",
#   "cf_xarray",
#   "cf_units",
# ]
# ///
"""Fetch a CMIP6 climate-projection dataset from the public Pangeo Google Cloud catalog and write a weather-skills envelope Zarr."""

import argparse
import calendar
import json
import re
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# Third-party imports are at module top so a missing inline dependency fails the
# script immediately, before any argument parsing or network access.
import cf_units
import cf_xarray  # noqa: F401  (registers the .cf accessor used below)
import gcsfs
import pandas as pd
import xarray as xr

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.6"

# Public, credential-free Pangeo CMIP6 collection on Google Cloud. The catalog
# CSV maps facet combinations to a Zarr store path (`zstore`); data is read
# anonymously.
_CATALOG_URL = "https://storage.googleapis.com/cmip6/pangeo-cmip6.csv"

# CF Conventions version this fetcher stamps on the output. CMIP6 source stores
# carry an older inherited value (e.g. "CF-1.7 CMIP-6.0 UGRID-1.0"); the envelope
# transform repairs the dataset to the current CF release and re-stamps it.
_CF_CONVENTIONS = "CF-1.13"

# --- Source -> output transforms ---
#
# Everything not listed here is passed through from the raw CMIP6 source
# verbatim. The transforms this fetcher applies are:
#
#   VARIABLE/COORD RENAMES:
#     - `lat` -> `latitude`
#     - `lon` -> `longitude`
#     (the data variable keeps its CMIP6 name, e.g. `tas`, `pr`)
#
#   LONGITUDE NORMALIZATION:
#     - The source 0..360 longitude axis is mapped onto [-180, 180) and the
#       dataset is re-sorted ascending by longitude (via `_normalize_longitude`,
#       called in the pipeline).
#
#   UNITS:
#     - PASS THROUGH VERBATIM. The source CF units string on the data variable is
#       forwarded unchanged; it is only validated as udunits-parseable
#       (cf_units.Unit) before write, never remapped or converted.
#
#   GLOBAL ATTRS:
#     - `Conventions` is OVERWRITTEN to the CF release above (_CF_CONVENTIONS).
#     - All other source CMIP6 global attrs are PRESERVED.
#     - `history` has one line APPENDED describing this subset.
#     - `weather_skills_source` and `weather_skills_history` keys are ADDED.
#
#   BOUNDS (structural):
#     - Every `*_bnds` / `*_bounds` cell-bounds variable is DROPPED, the orphaned
#       bounds index dim is removed, and each variable's now-dangling `bounds`
#       attr is STRIPPED (the weather-skills envelope carries no cell bounds).
#
#   standard_name / long_name:
#     - PRESERVED from source; this skill does not assign them. Coord CF attrs
#       (standard_name/units/axis on latitude/longitude/time) are only filled
#       via setdefault when absent after the rename, leaving source values intact.

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
        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            # compatibility read for the rhiza_ attr prefix; scheduled for removal
            raw = ds.attrs.get("weather_skills_history") or ds.attrs.get("rhiza_history")
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
        # A present-but-non-array value is malformed under the weather_skills_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed weather_skills_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


def _store_is_complete(out: Path, variable: str) -> bool:
    """Cheaply verify a candidate cache store is a fully written, readable Zarr.

    A previous run interrupted mid-write can leave a directory whose
    `weather_skills_history` is present (it is a top-level attr written early) but whose
    array data is absent or truncated. Honoring such a store as a cache hit would
    skip the fetch and leave the caller with a broken output. Re-open the store
    consolidated, confirm the requested variable's dims have non-zero extent, then
    read one corner cell of the variable: a truncated store can keep intact
    metadata while a chunk is missing, so a metadata-only check is not enough. The
    corner read forces the backing chunk to load; a missing/corrupt chunk raises
    and is caught as incomplete.
    """
    try:
        with xr.open_zarr(out, consolidated=True) as ds:
            if variable not in ds.data_vars:
                return False
            if not all(ds.sizes.get(d, 0) > 0 for d in ds[variable].dims):
                return False
            corner = {d: 0 for d in ds[variable].dims}
            ds[variable].isel(**corner).compute()
            return True
    except Exception:  # noqa: BLE001
        # Any failure to open the store or decode the corner chunk -- a truncated
        # or corrupt chunk surfaces as a backend-specific decompression error
        # (RuntimeError), not just OSError -- means the store is not a trustworthy
        # cache hit, so re-fetch.
        return False


def _cache_hit(out: Path, entry: dict, variable: str) -> bool:
    """Return True if the zarr at `out` was produced by this same entry.

    Requires both a matching provenance entry AND a structurally complete store,
    so a partially written store from an interrupted run is not trusted.
    """
    if not out.exists():
        return False
    history = _load_history(out)
    if not history:
        return False
    existing_entry = history[0]
    matches = (
        existing_entry.get("skill") == entry["skill"]
        and existing_entry.get("version") == entry["version"]
        and existing_entry.get("args") == entry["args"]
        and existing_entry.get("input") == entry["input"]
    )
    if not matches:
        return False
    return _store_is_complete(out, variable)


def _ensure_coord_cf_attrs(ds):
    """Ensure latitude/longitude/time coords carry CF standard_name/units/axis.

    CMIP6 source coords already carry these; this fills only any that are absent
    after the rename so the rest of the source metadata survives untouched.
    """
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


def _drop_bounds(ds):
    """Drop every `*_bnds` bounds variable and the dangling `bounds` attr it leaves.

    The weather-skills envelope does not carry cell bounds. Removing the bounds variables
    without also clearing the coords' `bounds` attrs would leave each coord
    pointing at an absent variable, which is a CF section 7.1 violation
    (cf-xarray's bounds resolution and any CF checker would flag it). This drops
    the bounds variables and the now-orphaned `bnds` index dim, then strips the
    `bounds` attr from every variable so no dangling reference remains.
    """
    bounds_vars = [
        v for v in ds.variables if str(v).endswith("_bnds") or str(v).endswith("_bounds")
    ]
    if bounds_vars:
        ds = ds.drop_vars(bounds_vars)
    # The bounds dim ("bnds") becomes an orphan index coord once its bounds
    # variables are gone; drop it too if it is a coord with no remaining users.
    for dim_coord in ("bnds", "bounds", "nv"):
        if dim_coord in ds.coords and dim_coord not in ds.dims:
            ds = ds.drop_vars(dim_coord)
    # Remove every `bounds` attr; each one now points at a removed variable.
    for v in ds.variables:
        if "bounds" in ds[v].attrs:
            del ds[v].attrs["bounds"]
    return ds


def _normalize_longitude(ds):
    """Map a 0..360 longitude onto [-180, 180) and sort ascending, so an N/W/S/E
    bbox with negative west/east values selects correctly."""
    lon = ((ds["longitude"] + 180) % 360) - 180
    ds = ds.assign_coords(longitude=lon)
    return ds.sortby("longitude")


def _bbox_subset(ds, bbox: str):
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox.

    The slice direction follows each axis's own monotonic order. A bbox whose
    west > east crosses the antimeridian (e.g. a Pacific-spanning region): select
    the two longitude bands `lon >= west OR lon <= east` rather than the empty
    `slice(west, east)`, keeping the grid in monotonic-ascending longitude.
    """
    try:
        north, west, south, east = (float(x) for x in bbox.split("/"))
    except ValueError:
        print("Error: --bbox must be four decimal degrees N/W/S/E.", file=sys.stderr)
        sys.exit(2)
    lat = ds["latitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    ds = ds.sel(latitude=lat_slice)
    if west > east:
        # Antimeridian-crossing box: keep both longitude bands. drop=True removes
        # the interior cells while preserving the ascending longitude order.
        ds = ds.where((ds["longitude"] >= west) | (ds["longitude"] <= east), drop=True)
    else:
        lon = ds["longitude"].values
        lon_slice = slice(west, east) if lon[0] < lon[-1] else slice(east, west)
        ds = ds.sel(longitude=lon_slice)
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        print(
            f"Error: --bbox {bbox} selects no grid cells; check the extent and N/W/S/E order.",
            file=sys.stderr,
        )
        sys.exit(1)
    return ds


def _resolve_zstore(args) -> tuple:
    """Resolve the facet flags against the CMIP6 catalog to exactly one zstore.

    Returns (zstore, grid_label, version). Exits 2 with diagnostics on zero
    matches or an ambiguous grid; exits 1 with an actionable message if the
    catalog CSV cannot be downloaded.
    """
    try:
        df = pd.read_csv(_CATALOG_URL)
    except Exception as exc:  # noqa: BLE001 (network/parse failures vary by backend)
        print(
            f"Error: failed to download or parse the CMIP6 catalog from {_CATALOG_URL} "
            f"({type(exc).__name__}: {exc}). Check network access to "
            "storage.googleapis.com, then retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    mask = (
        (df.source_id == args.model)
        & (df.experiment_id == args.experiment)
        & (df.variable_id == args.variable)
        & (df.member_id == args.member)
        & (df.table_id == args.table)
    )
    sub = df[mask]
    if args.grid:
        sub = sub[sub.grid_label == args.grid]
    if sub.empty:
        # Diagnose against the model alone so the message points at the real
        # available facet values rather than a blank "not found".
        for_model = df[df.source_id == args.model]
        if for_model.empty:
            hint = f"unknown --model {args.model!r}; sample models: {sorted(df.source_id.unique())[:10]}"
        else:
            hint = (
                f"for model {args.model!r}: "
                f"experiments={sorted(for_model.experiment_id.unique())[:12]}; "
                f"variables(table {args.table})="
                f"{sorted(for_model[for_model.table_id == args.table].variable_id.unique())[:15]}; "
                f"members={sorted(for_model.member_id.unique())[:8]}"
            )
        print(
            f"Error: no CMIP6 dataset matches model={args.model} experiment={args.experiment} "
            f"variable={args.variable} member={args.member} table={args.table}"
            + (f" grid={args.grid}" if args.grid else "")
            + f".\n{hint}",
            file=sys.stderr,
        )
        sys.exit(2)
    grids = sorted(sub.grid_label.unique())
    if len(grids) > 1:
        print(
            f"Error: multiple grid_labels match: {grids}. Pass --grid to choose one.",
            file=sys.stderr,
        )
        sys.exit(2)
    # Several versions may remain for the same facets; take the latest.
    row = sub.sort_values("version").iloc[-1]
    # Withdrawn/retracted catalog entries can carry a NaN or empty zstore. Passing
    # that to get_mapper fails opaquely; validate it here and point at the facets.
    zstore = row["zstore"]
    if not isinstance(zstore, str) or not zstore.strip():
        print(
            f"Error: the matched CMIP6 entry (model={args.model} experiment={args.experiment} "
            f"variable={args.variable} member={args.member} table={args.table} "
            f"grid={grids[0]} version={row['version']}) has no zstore path "
            "(the catalog row is empty/withdrawn). Try different facets or another "
            "--grid/--member.",
            file=sys.stderr,
        )
        sys.exit(2)
    return zstore, grids[0], str(row["version"])


def _validate_units(ds, variable: str) -> None:
    """Fail loudly if the data variable's units are not udunits-parseable.

    The output claims CF-1.13 compliance, so the data variable must carry a
    udunits-valid `units` string. CMIP6 stores carry CF-correct units, but a
    missing or malformed value would make the CF claim false; emit an actionable
    error rather than write it.
    """
    units = ds[variable].attrs.get("units")
    if units is None:
        print(
            f"Error: variable {variable!r} has no `units` attribute; cannot write a "
            "CF-compliant store. The source dataset is missing CF units.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        cf_units.Unit(units)
    except ValueError as exc:
        print(
            f"Error: variable {variable!r} has units {units!r}, which is not a valid "
            f"udunits string ({exc}); refusing to write it under a CF-1.13 claim.",
            file=sys.stderr,
        )
        sys.exit(1)


def _verify_cf_decode(ds, variable: str) -> None:
    """Confirm cf-xarray can resolve the X/Y/T axes before writing.

    A write-side guard: if the coord attrs do not let cf-xarray identify the
    longitude (X), latitude (Y), and time (T) axes, the output would not be the
    CF-navigable store the envelope promises. Fail with an actionable message.
    """
    axes = ds.cf.axes
    missing = [name for name in ("X", "Y", "T") if name not in axes]
    if missing:
        print(
            f"Error: cf-xarray cannot resolve axes {missing} on the output "
            f"(resolved: {sorted(axes)}); the coord CF attrs are incomplete.",
            file=sys.stderr,
        )
        sys.exit(1)


def _normalize_calendar(cal: str) -> str:
    """Fold CF calendar aliases to a canonical name for comparison.

    CF/cftime treat three pairs of names as aliases of the same calendar, so a
    source labeled with one name and a written store labeled with its alias (or
    vice versa) are the same calendar and must compare equal:

    - `gregorian` -> `standard`
    - `365_day` -> `noleap`
    - `366_day` -> `all_leap`

    `proleptic_gregorian` is a genuinely DISTINCT calendar (it extrapolates the
    Gregorian rule before 1582), not an alias of `standard`, and xarray
    round-trips it verbatim, so it is left unmapped here — a coercion between it
    and `standard` is a real change to flag.
    """
    aliases = {
        "gregorian": "standard",
        "365_day": "noleap",
        "366_day": "all_leap",
    }
    return aliases.get(cal, cal)


def _verify_written_calendar(out: Path, variable: str, source_calendar: str) -> None:
    """Re-open the written store and confirm the time calendar was not coerced.

    xarray can silently coerce a non-standard CMIP6 calendar (noleap, 360_day)
    to a proleptic-gregorian "standard" calendar on write if the time encoding is
    not preserved, which would corrupt the date axis. Read the calendar back off
    the written store and fail if it does not match the source, folding the
    CF `gregorian`/`standard` alias so a correct store is not falsely rejected.
    """
    with xr.open_zarr(out, consolidated=True, decode_times=False) as ds:
        written = ds["time"].attrs.get("calendar") or ds["time"].encoding.get("calendar")
        units = ds["time"].attrs.get("units") or ds["time"].encoding.get("units")
    if written is None:
        print(
            "Error: the written store has no `calendar` on its time axis; the source "
            f"calendar {source_calendar!r} was not preserved.",
            file=sys.stderr,
        )
        sys.exit(1)
    if _normalize_calendar(str(written)) != _normalize_calendar(str(source_calendar)):
        print(
            f"Error: time calendar was coerced to {written!r} on write but the source "
            f"calendar is {source_calendar!r}; refusing to emit a corrupted date axis.",
            file=sys.stderr,
        )
        sys.exit(1)
    if units is None:
        print(
            "Error: the written store has no udunits `units` on its time axis.",
            file=sys.stderr,
        )
        sys.exit(1)


def _attach_bbox_value(argv):
    # argparse rejects a space-separated --bbox value that starts with '-'
    # (a bbox whose North latitude is negative). Rewrite `--bbox VAL` to
    # `--bbox=VAL` so both the space and equals forms parse.
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--bbox" and i + 1 < len(argv):
            out.append(f"--bbox={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_SKILL_VERSION}",
    )
    p.add_argument("--model", required=True, help="CMIP6 source_id (e.g. GFDL-CM4).")
    p.add_argument(
        "--experiment", required=True, help="CMIP6 experiment_id (e.g. historical, ssp245)."
    )
    p.add_argument(
        "--variable",
        "-v",
        required=True,
        help="CMIP6 variable_id (one variable per dataset, e.g. tas, pr).",
    )
    p.add_argument("--member", default="r1i1p1f1", help="CMIP6 member_id (default r1i1p1f1).")
    p.add_argument("--table", default="Amon", help="CMIP6 table_id (default Amon).")
    p.add_argument(
        "--grid",
        help="CMIP6 grid_label; required only when more than one matches the other facets.",
    )
    p.add_argument(
        "--start",
        required=True,
        help=(
            "Range start, inclusive. Either YYYY-MM-DD, 'now'/'today', 'latest', "
            "or an offset 'now-<int>{d|w}' / 'latest-<int>{d|w}' (w = 7 days). "
            "Absolute future dates are allowed (scenario experiments run to 2100)."
        ),
    )
    p.add_argument(
        "--end", required=True, help="Range end, inclusive. Same date grammar as --start."
    )
    p.add_argument("--bbox", help="Spatial subset N/W/S/E decimal degrees. Omit for the full grid.")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args(_attach_bbox_value(sys.argv[1:]))

    zstore, grid_label, version = _resolve_zstore(args)

    fs = gcsfs.GCSFileSystem(token="anon")
    mapper = fs.get_mapper(zstore)
    # CMIP6 stores use non-standard calendars (noleap, 360_day), so decode times
    # with cftime. Newer xarray wants a CFDatetimeCoder passed to decode_times;
    # older xarray only accepts the use_cftime kwarg. Open is lazy: only metadata
    # is read until a subset is written.
    try:
        time_coder = xr.coders.CFDatetimeCoder(use_cftime=True)
        ds = xr.open_zarr(mapper, consolidated=True, decode_times=time_coder)
    except AttributeError:
        ds = xr.open_zarr(mapper, consolidated=True, use_cftime=True)

    # Capture the source calendar and time units from the time encoding before
    # any transform so they can be re-asserted on the written store. xarray
    # populates encoding["calendar"] and encoding["units"] when decoding times
    # with cftime. units may legitimately be absent; if so it is omitted from the
    # write encoding and xarray regenerates a correct value for the decoded cftime
    # values (no fabricated epoch).
    source_calendar = ds["time"].encoding.get("calendar")
    source_time_units = ds["time"].encoding.get("units")
    if source_calendar is None:
        print(
            "Error: could not determine the source time calendar from the CMIP6 store; "
            "refusing to write a store whose calendar cannot be verified.",
            file=sys.stderr,
        )
        sys.exit(1)

    # This fetcher handles regular 1-D lat/lon grids only. Ocean/curvilinear
    # CMIP6 grids carry 2-D latitude/longitude over (i, j) index dims, which the
    # 1-D-lat/lon weather-skills envelope does not model; reprojecting them is a grid
    # transform for a dedicated skill, not this fetcher.
    if "lat" not in ds.dims or "lon" not in ds.dims:
        print(
            f"Error: {args.model}/{args.variable} ({grid_label}) is not on a regular 1-D "
            f"lat/lon grid (dims {tuple(ds.dims)}); this fetcher handles only regular "
            "lat/lon grids. Reprojecting a curvilinear grid is a separate grid transform.",
            file=sys.stderr,
        )
        sys.exit(2)

    def _latest() -> date:
        # The time axis is cftime under a possibly non-standard CF calendar
        # (360_day, noleap, all_leap, julian), where a day value can be invalid
        # for the stdlib (e.g. Feb 30 on 360_day). Clamp the day to the
        # stdlib-valid maximum for that year/month so date() never raises. This
        # value only seeds the relative-date grammar window; the real selection
        # is string slicing on the cftime index (ds.sel(time=slice(...))), so a
        # day clamped by a day or two is acceptable here.
        t = ds["time"].values.max()
        last_day = calendar.monthrange(t.year, t.month)[1]
        return date(t.year, t.month, min(t.day, last_day))

    start_date, end_date, log_line = _resolve_window(args.start, args.end, _latest)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    if log_line is not None:
        print(log_line, file=sys.stderr)

    entry = {
        "skill": "cmip6-fetch",
        "version": _SKILL_VERSION,
        "args": {
            "model": args.model,
            "experiment": args.experiment,
            "variable": args.variable,
            "member": args.member,
            "table": args.table,
            "grid": grid_label,
            "data_version": version,
            "bbox": args.bbox,
            "start": start_iso,
            "end": end_iso,
        },
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry, args.variable):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    if args.variable not in ds.data_vars:
        print(
            f"Error: variable {args.variable!r} not present in the dataset; "
            f"available: {sorted(ds.data_vars)}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Capture the rich CMIP6 global attrs before any selection so they survive
    # onto the envelope. The transform preserves them, overwrites only
    # `Conventions`, and appends a history line.
    source_globals = dict(ds.attrs)

    # Keep only the requested variable. Selecting ds[[variable]] drops the
    # *_bnds bounds variables; _drop_bounds then clears the now-dangling `bounds`
    # attrs the coords would otherwise still carry.
    ds = ds[[args.variable]]
    ds = ds.rename({"lat": "latitude", "lon": "longitude"})
    ds = _drop_bounds(ds)
    ds = _normalize_longitude(ds)
    if args.bbox:
        ds = _bbox_subset(ds, args.bbox)

    ds = ds.sel(time=slice(start_iso, end_iso))
    if ds.sizes.get("time", 0) == 0:
        print(
            f"Error: {args.model}/{args.experiment}/{args.variable} has no data in "
            f"{start_iso}..{end_iso} (dataset time range may not cover the window).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Fetching cmip6 {args.model}/{args.experiment}/{args.member}/{args.table}/"
        f"{args.variable}/{grid_label} {start_iso}..{end_iso}",
        file=sys.stderr,
    )

    # Global attrs: preserve the source CMIP6 globals, overwrite Conventions to
    # the current CF release, append a history line, and add the weather_skills_* keys.
    history_line = (
        f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} cmip6-fetch: "
        f"subset {args.variable} to {start_iso}..{end_iso}"
        + (f" bbox {args.bbox}" if args.bbox else "")
        + f"; mapped onto the weather-skills envelope and re-stamped {_CF_CONVENTIONS}."
    )
    prior_history = source_globals.get("history", "")
    new_globals = dict(source_globals)
    new_globals["Conventions"] = _CF_CONVENTIONS
    new_globals["history"] = (
        (prior_history + "\n" + history_line) if prior_history else history_line
    )
    new_globals["weather_skills_source"] = (
        f"cmip6:{args.model}/{args.experiment}/{args.member}/"
        f"{args.table}/{args.variable}/{grid_label}"
    )
    new_globals["weather_skills_history"] = json.dumps([entry], sort_keys=True)
    ds.attrs = new_globals

    _ensure_coord_cf_attrs(ds)
    _validate_units(ds, args.variable)
    _verify_cf_decode(ds, args.variable)

    # Per-variable encoding is not part of the envelope contract; clear it so the
    # output is written with this skill's own codecs. The time axis is the
    # exception: its source `calendar` (and `units` when the source carried them)
    # must be carried into the write encoding so the non-standard CMIP6 calendar
    # is not coerced. A _FillValue, if present, belongs in the write encoding, not
    # in attrs, and is restored only on data variables -- CF discourages a
    # _FillValue on coordinate variables, so coords are cleared outright.
    for v in ds.variables:
        fill = ds[v].encoding.get("_FillValue") if v in ds.data_vars else None
        ds[v].encoding = {}
        if fill is not None:
            ds[v].encoding["_FillValue"] = fill
    # Omit units when the source did not carry them; xarray then generates a
    # correct udunits string for the decoded cftime values rather than us
    # inventing an epoch.
    if source_time_units is not None:
        ds["time"].encoding["units"] = source_time_units
    ds["time"].encoding["calendar"] = source_calendar

    if out.exists():
        if out.is_dir():
            shutil.rmtree(out)
        else:
            out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(out, mode="w", consolidated=True)

    # Verify on the WRITTEN store that the source calendar survived; do not
    # assume the write preserved it.
    _verify_written_calendar(out, args.variable, source_calendar)

    print(f"Wrote: {args.output} ({dict(ds.sizes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
