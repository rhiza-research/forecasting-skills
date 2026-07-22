# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "xarray",
#   "zarr",
#   "gcsfs",
#   "numpy",
#   "cf_xarray",
#   "cf_units",
#   "cftime",
# ]
# ///
"""Fetch ARCO-ERA5 reanalysis from the public Google Cloud Zarr and write a weather-skills envelope Zarr."""

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.8"

# Public, credential-free ARCO-ERA5 analysis-ready store: 0.25 deg equiangular
# lat/lon, hourly, dims (time, latitude, longitude, level). Opened anonymously.
# Path is the published value from the arco-era5 README, not a guess.
_ARCO_STORE = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
_STORAGE_OPTIONS = {"token": "anon"}

# Provenance recorded in the output's global `history` attr and `source` attr.
_ARCO_REFERENCE = "https://github.com/google-research/arco-era5"
_ARCO_INSTITUTION = "ECMWF (ERA5 reanalysis), republished as ARCO-ERA5 by Google Research"

# --- Source -> output transforms ---
# Pass-through is the default: source values, coords, and attrs are forwarded
# unchanged unless listed below. The only transforms this fetcher applies are:
#
#   1. Unit fixups (data vars), value-preserving same-unit relabels via
#      `_UNIT_FIXUPS`: "(0 - 1)" -> "1" and "dimensionless" -> "1" (the
#      dimensionless unit spelled for udunits), "m of water equivalent" -> "m"
#      (water-equivalent depth in meters). Same unit made to comply with
#      udunits; no numeric conversion. All other source units pass through.
#
#   2. Longitude normalization (`_normalize_longitude`): ERA5's native 0..360
#      longitude is mapped onto [-180, 180) and the axis re-sorted ascending so
#      negative-west/east bboxes select correctly. Coordinate relabel of the
#      same grid; sample values are unchanged.
#
#   3. Data-var standard_name / long_name (`_stamp_data_var_attrs`): source
#      passthrough plus a curated map. `long_name` and `standard_name` are
#      forwarded from the source when present; a variable with no source
#      `standard_name` gets one only from `_CURATED_STANDARD_NAME`, else carries
#      none (CF permits omission); `long_name` falls back to the variable name
#      only when the source omits it. The source attr block is otherwise
#      replaced so GRIB bookkeeping does not ride along.
#
#   4. Level coord attrs (`_stamp_coord_attrs`): the `level` units label
#      "Hectopascal(hPa)" is relabeled to "hPa" (values already in hPa; relabel,
#      not conversion) and standard_name=air_pressure, positive=down, axis=Z,
#      long_name="pressure level" are set. Latitude/longitude/time coords get
#      their CF standard_name/units/axis stamped likewise.

# Source `units` strings the ARCO store carries that are the SAME unit spelled in
# a form udunits won't parse; each is rewritten to the udunits spelling of the
# identical unit (same value, no conversion). UDUNITS-2 accepts the store's
# GRIB-style `**` exponent notation (e.g. `m s**-1`, `J m**-2`) as-is, so those
# pass through untouched. Every entry here is an unambiguous same-unit relabel:
#   "1" is the CF/udunits spelling of the dimensionless unit; "dimensionless"
#   and ERA5's range notation "(0 - 1)" (carried by albedos, cloud/vegetation
#   cover, land-sea mask, sea-ice fraction — all 0..1 fractions, several with
#   CF fraction standard_names) both name that same dimensionless unit. ERA5's
#   water-equivalent depths are meters.
# The store also carries "~" on a handful of vars (sub-gridscale-orography
# ratios, Charnock, and integer classification codes like soil_type /
# type_of_high_vegetation). "~" is ERA5's placeholder for "no stated unit", not
# a spelling of the dimensionless unit, and it spans category-index variables
# that are not dimensionless physical quantities — so it is NOT mapped; it
# passes through verbatim and the udunits check rejects it loudly rather than
# guessing it means "1".
_UNIT_FIXUPS = {
    "(0 - 1)": "1",
    "dimensionless": "1",
    "m of water equivalent": "m",
}

# Curated CF standard_name fills for common surface variables the ARCO store
# leaves without a `standard_name`. Each value is grounded in the store's own
# attrs: the pressure-level `temperature` var carries `air_temperature` with the
# same units (K), and the pressure-level `u/v_component_of_wind` vars carry
# `eastward_wind`/`northward_wind` with the same units (m s**-1). The map is
# deliberately small; a variable absent here simply carries no `standard_name`,
# which CF permits (units + long_name remain mandatory and present).
_CURATED_STANDARD_NAME = {
    "2m_temperature": "air_temperature",
    "2m_dewpoint_temperature": "dew_point_temperature",
    "10m_u_component_of_wind": "eastward_wind",
    "10m_v_component_of_wind": "northward_wind",
}

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


def _store_is_complete(zarr_path: Path, expected_vars) -> bool:
    """Return True if the Zarr at `zarr_path` is a complete, readable store.

    A `to_zarr` that crashed after the group metadata + attrs were flushed but
    before all chunks landed leaves a store whose `weather_skills_history` already matches
    the request — a stale-but-matching attr would otherwise be trusted as a cache
    hit even though the arrays are truncated or absent. This check guards against
    that: it opens the store consolidated and confirms each expected data
    variable is present and that a one-element corner slice actually computes
    (forcing a real chunk read). Any open/read failure, or a missing expected
    variable, means the store is incomplete and must be re-fetched.

    `expected_vars` is the requested variable list, or None for "all variables";
    when None, every data variable already in the store is probed (the store's
    own variable set is the only available expectation).
    """
    import xarray as xr

    try:
        with xr.open_zarr(zarr_path, consolidated=True) as ds:
            present = set(ds.data_vars)
            wanted = set(expected_vars) if expected_vars else present
            if not wanted or not wanted.issubset(present):
                return False
            for name in wanted:
                var = ds[name]
                # Read a single corner element to force a chunk read; a truncated
                # store raises here rather than returning a value.
                corner = {dim: 0 for dim in var.dims}
                var.isel(**corner).compute()
    except Exception:  # noqa: BLE001 — any failure means the store is not usable as a cache hit
        return False
    return True


def _cache_hit(out: Path, entry: dict, expected_vars) -> bool:
    """Return True if the zarr at `out` was produced by this same entry AND is a
    complete, readable store.

    The history-attr match alone is not sufficient: a partial prior write can
    leave a matching `weather_skills_history` over truncated arrays. `_store_is_complete`
    confirms the arrays are actually present and readable before the prior output
    is trusted; an incomplete store is treated as a miss and overwritten.
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
    if not _store_is_complete(out, expected_vars):
        print(
            f"Note: {out} matches the request but is an incomplete/unreadable store "
            "(likely a prior interrupted write); re-fetching.",
            file=sys.stderr,
        )
        return False
    return True


def _fix_units(units):
    """Return a candidate CF unit string for an ARCO source `units` value, or
    None when the source carries no units.

    UDUNITS-2 accepts the store's GRIB-style exponent notation directly, so only
    the handful of non-parseable placeholders in `_UNIT_FIXUPS` are rewritten;
    everything else is passed through unchanged. A missing/empty source units
    value returns None rather than a fabricated dimensionless "1" — silently
    relabeling a units-less variable as dimensionless would manufacture false
    CF compliance, so the caller fails loudly on None instead.
    """
    if not units:
        return None
    return _UNIT_FIXUPS.get(units, units)


def _udunits_valid(units: str) -> bool:
    """Return True if `units` parses as a UDUNITS-2 unit string.

    cf_units.Unit raises ValueError for a non-parseable unit; any such failure
    (and any other parse-time error) means the string is not udunits-valid.
    """
    import cf_units

    try:
        cf_units.Unit(units)
    except Exception:  # noqa: BLE001 — cf_units signals an unparseable unit by raising
        return False
    return True


def _stamp_data_var_attrs(ds) -> None:
    """Stamp CF `units` (mandatory), `long_name` (mandatory), and `standard_name`
    (when a valid CF value applies) on every data variable, validating each final
    `units` string against udunits.

    Source attrs from the ARCO store are used when present: `long_name` and
    `standard_name` are authored by the ERA5->Zarr pipeline and forwarded as-is,
    `units` is forwarded after the small udunits fixup. A variable with no source
    `standard_name` gets one only from the curated map; otherwise it carries none
    (CF permits omission). `long_name` falls back to the variable name only when
    the source omits it; it never masks a units failure.

    Output is written under `Conventions="CF-1.13"`, so every data variable's
    final `units` must parse as a UDUNITS-2 unit. A variable whose source units
    are missing, or are non-parseable and not covered by `_UNIT_FIXUPS`, raises
    ValueError naming the variable and the offending string rather than writing
    an invalid (or fabricated-dimensionless) unit under a false CF claim.
    """
    for name in ds.data_vars:
        src = ds[name].attrs
        raw_units = src.get("units")
        units = _fix_units(raw_units)
        if units is None:
            raise ValueError(
                f"data variable {name!r} has no source `units`; refusing to write it "
                "under Conventions=CF-1.13. A genuinely dimensionless quantity must "
                'carry units "1" at the source; a missing-units variable is not '
                "silently relabeled dimensionless. Select a variable that carries units."
            )
        if not _udunits_valid(units):
            raise ValueError(
                f"data variable {name!r} has units {units!r} that are not udunits-valid "
                f"(source units were {raw_units!r}); refusing to write it under "
                "Conventions=CF-1.13. Add a CF-valid mapping for this unit to the "
                "fixup table, or select a variable whose units parse."
            )
        long_name = src.get("long_name") or str(name)
        standard_name = src.get("standard_name") or _CURATED_STANDARD_NAME.get(name)
        # Replace the source attr block so GRIB bookkeeping (short_name, etc.)
        # does not ride along into the envelope.
        new_attrs = {"units": units, "long_name": long_name}
        if standard_name:
            new_attrs["standard_name"] = standard_name
        ds[name].attrs = new_attrs


def _stamp_coord_attrs(ds) -> None:
    """Stamp CF standard_name/units/axis on spatial, time, and level coords."""
    if "latitude" in ds.coords:
        ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    if "longitude" in ds.coords:
        ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    if "time" in ds.coords:
        ds["time"].attrs["standard_name"] = "time"
        ds["time"].attrs["axis"] = "T"
    if "level" in ds.coords:
        # ARCO stores `level` as integer pressure in hPa with a non-CF units
        # label ("Hectopascal(hPa)"). The values are already hPa, so this is a
        # relabel, not a conversion; CF orders pressure increasing downward.
        ds["level"].attrs.update(
            standard_name="air_pressure",
            units="hPa",
            positive="down",
            axis="Z",
            long_name="pressure level",
        )


def _global_attrs(start_iso: str, end_iso: str) -> dict:
    """Build the CF-1.13 global attrs for the output store."""
    stamped = datetime.now(UTC).isoformat(timespec="seconds")
    return {
        "Conventions": "CF-1.13",
        "title": f"ARCO-ERA5 reanalysis {start_iso}..{end_iso}",
        "institution": _ARCO_INSTITUTION,
        "source": _ARCO_STORE,
        "references": _ARCO_REFERENCE,
        "history": f"{stamped}: fetched by arco-era5-fetch {_SKILL_VERSION}",
    }


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

    Latitude is sliced following the axis's own monotonic order (ERA5 latitude is
    descending), so the same bbox works regardless of axis order.

    Longitude is normalized to ascending [-180, 180) upstream. A bbox with
    west <= east selects the contiguous span [west, east]. A bbox with
    west > east crosses the antimeridian (e.g. 10/170/-10/-170): the requested
    span wraps past +180/-180, so it is selected as the union `lon >= west` OR
    `lon <= east`, dropping the interior (non-selected) band while preserving the
    grid's native ascending longitude order. The result is monotonic ascending
    and holds the two dateline-flanking spans (the high-positive cells near +180
    and the negative cells near -180) in that ascending order; they are not
    physically contiguous across the dateline, but the coordinate stays sorted so
    downstream `.sel(slice(...))` tools keep working. Selecting that as a single
    slice(west, east) would instead pick the empty or inverted interval, which is
    the bug this branch avoids.
    """
    try:
        north, west, south, east = (float(x) for x in bbox.split("/"))
    except ValueError:
        print("Error: --bbox must be four decimal degrees N/W/S/E.", file=sys.stderr)
        sys.exit(2)
    lat = ds["latitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    ds = ds.sel(latitude=lat_slice)

    if west <= east:
        # Contiguous longitude span. Slice in the axis's own ascending order.
        lon = ds["longitude"].values
        lon_slice = slice(west, east) if len(lon) == 0 or lon[0] < lon[-1] else slice(east, west)
        ds = ds.sel(longitude=lon_slice)
    else:
        # Antimeridian crossing (west > east): the span runs west .. +180 and
        # -180 .. east. Select the union `lon >= west` OR `lon <= east` with a
        # boolean mask and drop the interior band, keeping the grid's native
        # ascending longitude order. This preserves a monotonic ascending
        # coordinate (the high-positive cells near +180 come first, the negative
        # cells near -180 follow), so downstream tools that assume a sorted
        # longitude and use `.sel(slice(...))` keep working. A concat of the two
        # halves would instead jump downward at the seam and break them.
        lon = ds["longitude"]
        ds = ds.where((lon >= west) | (lon <= east), drop=True)

    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        antimeridian = west > east
        if antimeridian:
            print(
                f"Error: --bbox {bbox} crosses the antimeridian (west {west} > east {east}) "
                "but selects no grid cells; check the N/S extent and that west/east "
                "bracket the intended dateline-crossing span.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: --bbox {bbox} selects no grid cells; check the extent and N/W/S/E order.",
                file=sys.stderr,
            )
        sys.exit(1)
    return ds


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
    args = p.parse_args(_attach_bbox_value(sys.argv[1:]))

    # Reject a malformed date token before any network call: parse both endpoints
    # for syntax now (no `latest` resolution, which needs the store). A bad token
    # exits 2 here, before the store is opened, per the CONVENTIONS.md grammar.
    for value in (args.start, args.end):
        try:
            _parse_token(value)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)

    import xarray as xr

    # Lazy open with chunks=None: reads only metadata, so `latest` resolution and
    # the cache check run before any array bytes are pulled, and xarray's lazy
    # indexing prunes to the bbox/time/variable selection before reading — so only
    # that selection is materialized. (A dask-backed open forces the longitude
    # normalization sort to pull the whole global grid per step, which is far slower
    # for a bbox request with no memory benefit; the selection itself is the bound.)
    # Store-open failures (network, bad path, gcsfs error) surface as a one-line
    # actionable message rather than a raw traceback.
    try:
        ds = xr.open_zarr(_ARCO_STORE, storage_options=_STORAGE_OPTIONS, chunks=None)
    except Exception as exc:  # noqa: BLE001
        print(
            f"Error: could not open the ARCO-ERA5 store at {_ARCO_STORE} "
            f"({type(exc).__name__}: {exc}). Check network access to Google Cloud "
            "Storage; the store is read anonymously, so no credentials are needed.",
            file=sys.stderr,
        )
        sys.exit(1)

    # `latest` resolves to the newest date that actually has data. The store's
    # `time` coordinate is pre-allocated far into the future (empty placeholder
    # slots out to ~2050), so its max is not the data edge. The real extent is
    # published in the store's global attrs: `valid_time_stop_era5t` marks the
    # near-real-time (ERA5T) edge and `valid_time_stop` the finalized-ERA5 edge.
    # Both are inclusive (data exists through that date); the near-real-time edge
    # is preferred. Fall back to the time-coord max only if neither attr is
    # present or parseable. Memoized so it is computed at most once and only when
    # a token references `latest`. These marker attrs are trusted as the data
    # edge and are not cross-checked against the actually-filled `time` slots, so
    # in the rare case the store publishes a marker ahead of its written data, a
    # `latest`-anchored request resolves to a date with no time steps and exits
    # with the existing clean "no data in <start>..<end>" error rather than
    # silently returning wrong data.
    _latest_cache: dict = {}

    def _latest() -> date:
        if "v" not in _latest_cache:
            for attr in ("valid_time_stop_era5t", "valid_time_stop"):
                raw = ds.attrs.get(attr)
                if raw and _ABS_DATE_RE.match(str(raw).strip()):
                    try:
                        _latest_cache["v"] = date.fromisoformat(str(raw).strip())
                        break
                    except ValueError:
                        pass
            else:
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
        "version": _SKILL_VERSION,
        "args": args_dict,
        "input": None,
    }
    out = Path(args.output)
    if _cache_hit(out, entry, args.variable):
        print(
            f"Cache hit: {args.output} already matches requested params; skipping fetch.",
            file=sys.stderr,
        )
        return

    # Selection: variable -> bbox -> time. Each step prunes before the eager load.
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
            "Note: no --variable given; selecting all data variables (large). Pass -v to restrict.",
            file=sys.stderr,
        )

    ds = _normalize_longitude(ds)
    if args.bbox:
        ds = _bbox_subset(ds, args.bbox)

    # Inclusive whole-day window over the hourly time axis.
    ds = ds.sel(time=slice(np.datetime64(f"{start_iso}T00:00"), np.datetime64(f"{end_iso}T23:59")))
    if ds.sizes.get("time", 0) == 0:
        print(f"Error: ARCO-ERA5 has no data in {start_iso}..{end_iso}.", file=sys.stderr)
        sys.exit(1)

    print(
        f"Fetching arco-era5 {start_iso}..{end_iso}",
        file=sys.stderr,
    )

    ds.attrs.clear()
    ds.attrs.update(_global_attrs(start_iso, end_iso))
    ds.attrs["weather_skills_source"] = "arco-era5"
    ds.attrs["weather_skills_history"] = json.dumps([entry], sort_keys=True)
    _stamp_coord_attrs(ds)
    # Validate and stamp every data variable's CF attrs. A variable whose final
    # `units` cannot be made udunits-valid (missing, or non-parseable and not in
    # the fixup map) fails here rather than being written under a false CF claim.
    try:
        _stamp_data_var_attrs(ds)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Per-variable encoding is not part of the envelope contract; clear it so the
    # output is written with this skill's own codecs. The time coord's CF units +
    # calendar are set explicitly in the write encoding so the on-disk time axis
    # is self-describing per CF.
    for v in ds.variables:
        ds[v].encoding = {}
    time_encoding = {}
    if "time" in ds.coords:
        ref = start_iso
        ds["time"].encoding = {
            "units": f"hours since {ref} 00:00:00",
            "calendar": "proleptic_gregorian",
        }
        time_encoding = ds["time"].encoding

    # Write-side CF decode check: re-encode then decode the in-memory dataset with
    # cf_xarray's machinery and confirm every axis resolves. This catches a coord
    # attr regression before the store is written rather than at read time.
    import cf_xarray  # noqa: F401 — registers the .cf accessor

    try:
        axes = ds.cf.axes
        for required in ("X", "Y", "T"):
            if required not in axes:
                raise ValueError(f"CF axis {required} did not resolve from coord attrs")
        if "level" in ds.coords and "Z" not in ds.cf.axes and "vertical" not in ds.cf.coordinates:
            raise ValueError("level present but did not resolve as a CF vertical coordinate")
    except Exception as exc:  # noqa: BLE001
        print(
            f"Error: the output failed the CF decode check before writing ({exc}). "
            "This is a bug in the fetcher's CF stamping, not a data problem.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        # Clear any prior output. A Zarr store is a directory, but the path may
        # already exist as a regular file (or symlink): rmtree raises
        # NotADirectoryError on a file, so branch on the path kind and unlink a
        # file vs. remove a directory tree.
        if out.is_dir():
            shutil.rmtree(out)
        elif out.exists():
            out.unlink()
        out.parent.mkdir(parents=True, exist_ok=True)
        encoding = {"time": time_encoding} if time_encoding else None
        ds.to_zarr(out, mode="w", consolidated=True, encoding=encoding)
    except MemoryError:
        # Reactive backstop only: the eager `.to_zarr()` load materializes the
        # whole pruned selection into host memory at once, so a very large
        # selection can exhaust memory. This handler is best-effort — under Linux
        # memory overcommit an OOM typically arrives as a SIGKILL (the process is
        # killed outright, printing only "Killed"), so a catchable MemoryError is
        # not guaranteed. When it is catchable, surface it actionably so a caller
        # narrows the request rather than blindly retrying the same dead call.
        print(
            "Error: ran out of memory materializing the selection. "
            "Narrow it with -v, --bbox, or a shorter window.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(
            f"Error: failed while reading from the ARCO-ERA5 store or writing {args.output} "
            f"({type(exc).__name__}: {exc}).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
