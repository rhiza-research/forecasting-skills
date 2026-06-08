# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "earthaccess",
#   "h5py",
#   "xarray",
#   "zarr",
#   "numpy",
#   "cf_xarray",
#   "cf_units",
#   "cftime",
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

import cf_units
import cf_xarray  # noqa: F401  -- registers the `.cf` accessor used in the write-side decode check
import earthaccess
import h5py
import numpy as np
import xarray as xr
from earthaccess.exceptions import LoginAttemptFailure, LoginStrategyUnavailable

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_RHIZA_SKILL_VERSION = "0.1.1"

_SHORT_NAME = "SPL3SMP_E"
_FILL = -9999.0
# How far back the `latest` search looks for the newest published granule. SMAP
# L3 runs a few days behind realtime; 30 days covers the lag plus short gaps.
_LATEST_LOOKBACK_DAYS = 30
# Parses the YYYYMMDD acquisition date out of a granule filename, e.g.
# SMAP_L3_SM_P_E_20240601_R19240_001.h5.
_GRANULE_DATE_RE = re.compile(r"_(\d{8})_")

# CF standard_name for the soil_moisture data variable, confirmed against the CF
# standard-name table v93 (current at implementation):
# `volume_fraction_of_condensed_water_in_soil` has canonical_units "1". The SMAP
# file labels the same quantity `cm3/cm3` (a dimensionless volumetric ratio),
# which parses under udunits and is therefore already CF-conformant. Per the units
# principle, the source units pass through verbatim onto the output variable
# (read-and-validate, not relabel); only the standard_name is asserted here.
_SM_STANDARD_NAME = "volume_fraction_of_condensed_water_in_soil"

# --- Source -> output transforms ---
# Pass-through is the default for everything not listed here (values, fill, dtype,
# time). The divergences this skill introduces between the raw SPL3SMP_E granule
# and the output Envelope are:
#
#   - soil_moisture units: PASS THROUGH VERBATIM. The granule's `units` attribute
#     (cm3/cm3, a dimensionless volumetric ratio) is read off the dataset and
#     written unchanged on the output variable after udunits validation. No remap,
#     no relabel, no numeric conversion.
#   - Geolocation 2-D -> 1-D: SPL3SMP_E stores latitude/longitude as 2-D EASE-Grid
#     arrays that are row-constant in latitude and column-constant in longitude;
#     they are reduced to 1-D latitude/longitude coordinate vectors
#     (_reduce_geolocation), checked for row/column constancy within tolerance.
#   - soil_moisture standard_name: assigned `volume_fraction_of_condensed_water_in_soil`
#     from the CF standard-name table (the granule carries no CF standard_name).
#   - grid_mapping: a `latitude_longitude` CF grid_mapping container variable is
#     added to carry the WGS84 geographic CRS for the lat/lon presentation of the
#     EASE-Grid 2.0 cells.
#
# Longitude is left in the granule's native order/range; no lon normalization is
# applied.

# CF global attrs describing the source product.
_CF_CONVENTIONS = "CF-1.13"
_CF_TITLE = "SMAP Enhanced L3 Radiometer Global Daily Soil Moisture"
_CF_SOURCE = "SMAP SPL3SMP_E (Enhanced L3 Radiometer 9 km EASE-Grid 2.0 soil moisture)"
_CF_INSTITUTION = "NASA National Snow and Ice Data Center DAAC"
_CF_REFERENCES = "https://nsidc.org/data/spl3smp_e"

# Single actionable, credential-free message emitted on any Earthdata auth
# failure. Shared by `_auth_fail` (paths with no store on disk) and `_fail`
# (mid-stream paths that must clean up a partial store first) so the wording
# stays identical regardless of which path emits it.
_AUTH_FAIL_MSG = (
    "Error: Earthdata authentication failed; configure EARTHDATA_USERNAME/"
    "EARTHDATA_PASSWORD or a urs.earthdata.nasa.gov entry in ~/.netrc, then retry."
)

# udunits time encoding for the time coordinate. Carried in the WRITE ENCODING
# (set after the per-variable .encoding clear) so the clear cannot drop it.
_TIME_UNITS = "days since 1970-01-01 00:00:00"
_TIME_CALENDAR = "proleptic_gregorian"

# Name of the CF grid_mapping container variable. SPL3SMP_E geolocation is the
# geographic (lat/lon) presentation of EASE-Grid 2.0 cells; the 1-D lat/lon
# coordinate vectors carry the true (non-uniform) cell-center positions, so the
# CRS is a plain geographic latitude_longitude.
_GRID_MAPPING_NAME = "latitude_longitude"

# Upper bound on a relative offset's resolved day count. 36525 days (~100 years)
# is far beyond any real window yet small enough that the date arithmetic cannot
# raise OverflowError. Rejecting above this cap keeps the failure pre-network.
_MAX_OFFSET_DAYS = 36525

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


def _parse_bbox(bbox: str) -> tuple:
    """Parse an N/W/S/E bbox string into four floats, exiting 2 on a bad shape."""
    try:
        north, west, south, east = (float(x) for x in bbox.split("/"))
    except ValueError:
        print("Error: --bbox must be four decimal degrees N/W/S/E.", file=sys.stderr)
        sys.exit(2)
    return north, west, south, east


def _bbox_subset(ds, bbox: str):
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox.

    Latitude follows its own monotonic order (SMAP latitude is descending), sliced
    north..south or south..north accordingly. For longitude (ascending in
    [-180, 180)):
      - an ordinary box (west <= east) is a plain slice(west, east);
      - an antimeridian-crossing box (west > east, e.g. 12/170/-6/-170) selects the
        union of the two bands lon >= west OR lon <= east via a boolean
        .where(..., drop=True), so the native ascending longitude order is
        preserved and the interior band between east and west is dropped.
    """
    north, west, south, east = _parse_bbox(bbox)
    lat = ds["latitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    ds = ds.sel(latitude=lat_slice)
    if west > east:
        ds = ds.where((ds["longitude"] >= west) | (ds["longitude"] <= east), drop=True)
    else:
        ds = ds.sel(longitude=slice(west, east))
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        # Raise rather than sys.exit: this runs inside the per-day loop, so the
        # caller routes it through `_fail` to clean up any partial store first.
        raise RuntimeError(
            f"--bbox {bbox} selects no grid cells; check the extent and N/W/S/E order."
        )
    return ds


def _granule_date(granule) -> date:
    """Parse the acquisition date from a granule's data-file name."""
    for url in granule.data_links():
        m = _GRANULE_DATE_RE.search(url.rsplit("/", 1)[-1])
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d").date()
    raise ValueError("could not parse a YYYYMMDD date from the granule file name")


def _reduce_geolocation(lat2d, lon2d, day_iso: str) -> tuple:
    """Reduce the 2-D EASE-Grid geolocation to 1-D lat/lon coordinate vectors.

    SPL3SMP_E stores latitude/longitude as 2-D arrays that are (within rounding)
    constant along one axis each: every row shares one latitude, every column one
    longitude. Before averaging to 1-D, verify that assumption per row/column; if
    a row's finite latitudes (or a column's finite longitudes) vary beyond
    tolerance, the grid is not the expected rectilinear EASE-Grid presentation, so
    fail with a clear message rather than silently averaging across genuinely
    different coordinates.
    """
    # Per-row latitude spread and per-column longitude spread over finite cells.
    with np.errstate(invalid="ignore"):
        lat_row_spread = np.nanmax(lat2d, axis=1) - np.nanmin(lat2d, axis=1)
        lon_col_spread = np.nanmax(lon2d, axis=0) - np.nanmin(lon2d, axis=0)
    # 9 km EASE-Grid 2.0 cell spacing is ~0.08 deg; a constant-lat row / constant-lon
    # column should vary far below that. Use 1e-3 deg (~100 m) as the tolerance.
    tol = 1e-3
    max_lat_spread = float(np.nanmax(lat_row_spread)) if lat_row_spread.size else 0.0
    max_lon_spread = float(np.nanmax(lon_col_spread)) if lon_col_spread.size else 0.0
    if max_lat_spread > tol or max_lon_spread > tol:
        # Raise rather than sys.exit: this runs (via _slice_from_file) inside the
        # per-day loop, so the caller routes it through `_fail` to clean up any
        # partial store first.
        raise RuntimeError(
            f"granule for {day_iso} is not row-constant-lat / col-constant-lon "
            f"within {tol} deg (max per-row lat spread {max_lat_spread:.5f}, max "
            f"per-col lon spread {max_lon_spread:.5f}); cannot reduce the 2-D "
            "EASE-Grid geolocation to 1-D coordinate vectors without distorting it."
        )
    lat1d = np.nanmean(lat2d, axis=1)
    lon1d = np.nanmean(lon2d, axis=0)
    # An all-fill/all-NaN geolocation row (or column) makes its nanmax-nanmin
    # spread NaN, and `NaN > tol` is False, so the spread guard above does not
    # catch it; nanmean of an all-NaN slice then yields a NaN coordinate value
    # (with a RuntimeWarning). A mixed-NaN row passes the spread guard too but is
    # fine for nanmean. Reject any non-finite reduced coordinate outright so a
    # store with a NaN lat/lon coordinate is never written.
    if not (np.isfinite(lat1d).all() and np.isfinite(lon1d).all()):
        # Raise rather than sys.exit: this runs (via _slice_from_file) inside the
        # per-day loop, so the caller routes it through `_fail` to clean up any
        # partial store first.
        raise RuntimeError(
            f"granule for {day_iso} has an all-fill geolocation row/column; "
            "the reduced 1-D latitude/longitude contains non-finite values and "
            "cannot be used as coordinate vectors."
        )
    return lat1d, lon1d


def _read_source_units(grp, day_iso: str) -> str:
    """Read the granule's soil_moisture `units` attribute for verbatim pass-through.

    SPL3SMP_E labels soil_moisture with a dimensionless volumetric ratio
    (cm3/cm3), which parses under udunits and is already CF-conformant, so the
    units pass through verbatim onto the output variable rather than being
    relabeled. Return the source units string unchanged (decoded if stored as
    bytes); the caller validates it under udunits before writing. A genuinely
    missing units attribute cannot be passed through, and fabricating one is not
    allowed, so fail with an actionable message rather than inventing a value.
    """
    raw = grp["soil_moisture"].attrs.get("units")
    if raw is None:
        # Raise rather than sys.exit: this runs (via _slice_from_file) inside the
        # per-day loop, so the caller routes it through `_fail` to clean up any
        # partial store first.
        raise RuntimeError(
            f"granule for {day_iso} has no units attribute on soil_moisture; "
            "cannot pass source units through verbatim and will not fabricate one."
        )
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return str(raw).strip()


def _slice_from_file(path: str, group: str, day_iso: str):
    """Read one SPL3SMP_E granule into a (latitude, longitude) DataArray.

    Raises a per-granule actionable error (caught by the caller) if the HDF5 file
    cannot be opened or the expected overpass group / soil_moisture / latitude /
    longitude dataset is missing.
    """
    try:
        with h5py.File(path, "r") as h:
            grp_name = f"Soil_Moisture_Retrieval_Data_{group}"
            if grp_name not in h:
                raise KeyError(
                    f"granule for {day_iso} has no group {grp_name!r} "
                    f"(overpass {group}); available groups: {list(h.keys())}"
                )
            grp = h[grp_name]
            for dataset in ("soil_moisture", "latitude", "longitude"):
                if dataset not in grp:
                    raise KeyError(
                        f"granule for {day_iso} group {grp_name!r} is missing the "
                        f"{dataset!r} dataset (overpass {group}); "
                        f"available datasets: {list(grp.keys())}"
                    )
            source_units = _read_source_units(grp, day_iso)
            sm = grp["soil_moisture"][:].astype("float64")
            lat2d = grp["latitude"][:].astype("float64")
            lon2d = grp["longitude"][:].astype("float64")
    except KeyError as exc:
        # KeyError str() is repr-quoted; exc.args[0] is the bare message.
        raise RuntimeError(
            f"failed to read SMAP granule for {day_iso} at {path}: {exc.args[0]}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"failed to read SMAP granule HDF5 for {day_iso} at {path}: {exc}"
        ) from exc

    sm = np.where(sm == _FILL, np.nan, sm)
    lat2d = np.where(lat2d == _FILL, np.nan, lat2d)
    lon2d = np.where(lon2d == _FILL, np.nan, lon2d)
    lat1d, lon1d = _reduce_geolocation(lat2d, lon2d, day_iso)

    da = xr.DataArray(
        sm,
        dims=("latitude", "longitude"),
        coords={"latitude": lat1d, "longitude": lon1d},
        name="soil_moisture",
        # Carry the granule's source units through to the CF stamping step, where
        # they are validated under udunits and written verbatim on the output.
        attrs={"units": source_units},
    )
    return da


def _is_auth_error(exc: Exception) -> bool:
    """Detect an Earthdata auth failure, primarily from the HTTP status.

    earthaccess surfaces auth failures as an HTTP 401/403 on search/download (the
    explicit earthaccess login-failure exception types are handled separately in
    `_login`). Prefer the status code, available either on a `response` object or
    as a `status`/`status_code` attribute on the exception itself. Broad keyword
    matching on the message ("login", "credential", "authenticat") misroutes
    non-auth failures — a network error, or a redirect URL that contains the word
    "login" — to the credential message, so it is not used. The narrow unambiguous
    HTTP phrases "unauthorized"/"forbidden" are kept only as a fallback for clients
    that put the status in the text but not on an attribute.
    """
    for status in (
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
    ):
        if status in (401, 403):
            return True
    msg = str(exc).lower()
    return "401 unauthorized" in msg or "403 forbidden" in msg


def _auth_fail() -> None:
    """Emit the single actionable, credential-free auth message and exit non-zero.

    Used by the pre-write paths (`_login`, `_search`, the initial `_download`)
    where no output store exists yet, so there is nothing to clean up. Mid-stream
    auth failures in the per-day loop instead go through `_fail(..., auth=True)`,
    which removes the partial store before emitting this same message.
    """
    print(_AUTH_FAIL_MSG, file=sys.stderr)
    sys.exit(1)


def _login() -> None:
    """Authenticate to Earthdata, converting any failure into the actionable message.

    earthaccess.login() consumes the credentials itself; this wrapper never reads,
    prints, or checks a credential value. It tries the non-interactive strategies
    in order — `environment` (EARTHDATA_USERNAME/PASSWORD or EARTHDATA_TOKEN) then
    `netrc` (~/.netrc) — and deliberately never falls through to `interactive`,
    which would block on a tty prompt. If neither strategy authenticates (creds
    absent or rejected), emit the one-line actionable message and exit non-zero.
    """
    for strategy in ("environment", "netrc"):
        try:
            auth = earthaccess.login(strategy=strategy)
        except LoginStrategyUnavailable:
            # This strategy's source (env vars / netrc entry) is not present; try
            # the next one.
            continue
        except LoginAttemptFailure:
            # Credentials were found but rejected by Earthdata.
            _auth_fail()
        except Exception as exc:
            if _is_auth_error(exc):
                _auth_fail()
            raise
        if getattr(auth, "authenticated", False):
            return
    # No strategy yielded an authenticated session.
    _auth_fail()


def _search(start_iso: str, end_iso: str):
    """Search CMR for SPL3SMP_E granules, mapping a 401/403 to the auth message."""
    try:
        return earthaccess.search_data(
            short_name=_SHORT_NAME,
            temporal=(start_iso, end_iso),
        )
    except Exception as exc:
        if _is_auth_error(exc):
            _auth_fail()
        raise


def _download(granules, local_path: str):
    """Download granules, mapping a 401/403 to the auth message."""
    try:
        return earthaccess.download(granules, local_path=local_path)
    except Exception as exc:
        if _is_auth_error(exc):
            _auth_fail()
        raise


def _discover_latest(lookback_days: int) -> date:
    """`latest` resolver: newest SPL3SMP_E granule date on or before today."""
    today = datetime.now(UTC).date()
    lookback_start = today - timedelta(days=lookback_days)
    results = _search(lookback_start.isoformat(), today.isoformat())
    dates = [d for d in (_granule_date(r) for r in results) if d <= today]
    if not dates:
        print(
            f"Error: no SMAP {_SHORT_NAME} granules in lookback window "
            f"{lookback_start.isoformat()}..{today.isoformat()}; cannot resolve 'latest'.",
            file=sys.stderr,
        )
        sys.exit(2)
    return max(dates)


def _validate_units(units: str) -> None:
    """Real udunits validation of the data-var units; fail loudly if invalid.

    Uses cf_units to parse the units string the way a CF-aware reader would. If
    the canonical units cannot be constructed, refuse to write rather than emit a
    false CF claim.
    """
    try:
        cf_units.Unit(units)
    except Exception as exc:  # cf_units raises ValueError on an unparseable units string
        print(
            f"Error: soil_moisture units {units!r} are not udunits-valid "
            f"({exc}); refusing to write a CF-noncompliant store.",
            file=sys.stderr,
        )
        sys.exit(1)


def _stamp_cf(ds, entry: dict) -> None:
    """Stamp full CF-1.13 metadata onto the dataset in place.

    Global Conventions/title/source/institution/references/history; coordinate
    standard_name/units/axis on lat/lon/time; soil_moisture standard_name +
    verbatim source units + long_name + grid_mapping; a latitude_longitude
    grid_mapping container variable. Validates the data-var units against udunits.
    The time udunits/calendar and the soil_moisture _FillValue are NOT set here —
    they live in the WRITE ENCODING so the per-variable .encoding clear cannot
    drop them.
    """
    # Source units carried through from _slice_from_file; passed through verbatim.
    source_units = ds["soil_moisture"].attrs["units"]
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    ds.attrs.clear()
    ds.attrs.update(
        Conventions=_CF_CONVENTIONS,
        title=_CF_TITLE,
        source=_CF_SOURCE,
        institution=_CF_INSTITUTION,
        references=_CF_REFERENCES,
        history=f"{now_iso}: fetched via smap-fetch v{_RHIZA_SKILL_VERSION}",
        rhiza_source="smap",
        rhiza_history=json.dumps([entry], sort_keys=True),
    )

    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")

    _validate_units(source_units)
    ds["soil_moisture"].attrs.update(
        standard_name=_SM_STANDARD_NAME,
        units=source_units,
        long_name=f"volumetric soil moisture ({source_units})",
        grid_mapping=_GRID_MAPPING_NAME,
    )

    # CF grid_mapping container: a geographic CRS for the lat/lon presentation of
    # the EASE-Grid 2.0 cells. The data values live in the coordinate vectors; the
    # container carries the CRS so a CF reader resolves the horizontal datum.
    # SPL3SMP_E is on EASE-Grid 2.0, which is defined on the WGS84 ellipsoid (the
    # 2.0 revision adopted WGS84; the original EASE-Grid used a spherical datum).
    # The values below are the WGS84 defining constants.
    ds[_GRID_MAPPING_NAME] = xr.DataArray(0, attrs={})
    ds[_GRID_MAPPING_NAME].attrs.update(
        grid_mapping_name="latitude_longitude",
        longitude_of_prime_meridian=0.0,
        semi_major_axis=6378137.0,
        inverse_flattening=298.257223563,
    )


def _cf_decode_check(ds) -> None:
    """Write-side decode check: confirm cf-xarray resolves the lat/lon/time axes.

    Fails loudly before writing if any of the three axes cannot be resolved from
    the CF attrs, so a stamping regression cannot silently ship a store that
    downstream cf-xarray-based skills can't read.
    """
    missing = []
    for axis in ("X", "Y", "T"):
        try:
            resolved = ds.cf.axes.get(axis)
        except Exception:
            resolved = None
        if not resolved:
            missing.append(axis)
    if missing:
        print(
            f"Error: cf-xarray could not resolve axes {missing} from the stamped "
            "CF attrs; refusing to write a store downstream skills cannot decode.",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_RHIZA_SKILL_VERSION}",
    )
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
    p.add_argument(
        "--bbox",
        help=(
            "Spatial subset N/W/S/E decimal degrees (optional). A box with "
            "west > east crosses the antimeridian. Resolve a country's bbox with "
            "resolve-region."
        ),
    )
    p.add_argument(
        "--overpass",
        choices=["AM", "PM"],
        default="AM",
        help="Half-orbit overpass group to read (AM = 6am descending, PM = 6pm ascending). Default AM.",
    )
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    out = Path(args.output)

    # `latest` discovery needs an earthaccess.login() before the CMR search.
    # Memoized so a window referencing `latest` at both ends discovers once; an
    # all-absolute or now-only window performs no discovery login.
    _latest_cache: dict = {}

    def _latest() -> date:
        if "v" not in _latest_cache:
            _login()
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

    requested_span = (end_date - start_date).days + 1

    print(f"Fetching SMAP {_SHORT_NAME} ({args.overpass}) {start_iso}..{end_iso}", file=sys.stderr)
    _login()
    results = _search(start_iso, end_iso)
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

    # Missing-day visibility: name any requested day that has no granule, then
    # proceed to write the days that do exist (rather than silently returning a
    # short result with no note). Distinguish trailing missing days (a contiguous
    # run at the end of the window, the usual publication-lag case) from interior
    # gaps (missing days with a later present day) so the message does not assert a
    # false "not yet published" cause for a genuine interior gap.
    present_days = sorted(in_window)
    requested_days = [start_date + timedelta(days=i) for i in range(requested_span)]
    missing_days = [d for d in requested_days if d not in in_window]
    if missing_days:
        last_present = present_days[-1]
        trailing = [d for d in missing_days if d > last_present]
        interior = [d for d in missing_days if d < last_present]
        parts = []
        if trailing:
            parts.append(
                "trailing (after the last available day, likely not yet "
                f"published): {', '.join(d.isoformat() for d in trailing)}"
            )
        if interior:
            parts.append(
                "interior gap (a day with no granule between available days): "
                f"{', '.join(d.isoformat() for d in interior)}"
            )
        print(
            f"WARNING: {len(missing_days)} requested day(s) have no SMAP granule "
            f"within {start_iso}..{end_iso}; writing the {len(present_days)} "
            f"available day(s) only. {'; '.join(parts)}.",
            file=sys.stderr,
        )

    # Stream one day at a time: download a granule, read its (latitude, longitude)
    # slice, then write/append it to Zarr as a single time step before moving to the
    # next day. Peak resident memory is bounded to one day's grid rather than the
    # whole window. Full CF metadata + rhiza_source/history are stamped on every
    # write because a to_zarr append rewrites the root group attrs from the
    # appended dataset; the entry is identical each time, so the final stamp is
    # stable. xarray appends only the time-varying soil_moisture along `time`; the
    # non-time latitude_longitude grid_mapping scalar and the static lat/lon coords
    # are written once on the first mode="w" write and left untouched by each
    # append (verified: no duplication, no error).
    #
    # Partial-store safety: the first day is written with mode="w" and subsequent
    # days append. If a write fails mid-stream the store is left truncated, and a
    # subsequent identical run would see a matching rhiza_history and treat the
    # truncated store as a complete cache hit. To prevent that, `store_created` is
    # flipped to True the moment this run starts writing (just before the first
    # mode="w" call, after any pre-existing store has been removed), and `_fail`
    # removes the store whenever that flag is set, so a partial store — including
    # one a failed first write may already have created — is never left behind to be
    # mistaken for a complete cache.
    store_created = False

    def _fail(msg: str, *, auth: bool = False) -> None:
        # Every mid-stream failure (auth, non-auth download error, h5py read
        # error, write error) converges here so the partial-store cleanup runs
        # exactly once regardless of failure kind. Pass auth=True for an auth
        # failure: the cleanup runs first, then the shared credential-free auth
        # message is emitted instead of `msg`.
        if store_created and out.exists():
            shutil.rmtree(out)
            print(
                f"Removed partial store {args.output} after a mid-stream failure "
                "so it is not mistaken for a complete cache on a later run.",
                file=sys.stderr,
            )
        print(_AUTH_FAIL_MSG if auth else msg, file=sys.stderr)
        sys.exit(1)

    total_time = 0
    with tempfile.TemporaryDirectory(prefix="smap-fetch-") as td:
        for d in present_days:
            day_iso = d.isoformat()
            try:
                files = _download([in_window[d]], local_path=td)
            except Exception as exc:
                if _is_auth_error(exc):
                    _fail(
                        f"Error: failed to download the SMAP granule for {day_iso}: {exc}",
                        auth=True,
                    )
                _fail(f"Error: failed to download the SMAP granule for {day_iso}: {exc}")
            if not files:
                _fail(f"Error: download returned no local file for the {day_iso} granule.")
            try:
                da = _slice_from_file(files[0], args.overpass, day_iso)
                if args.bbox:
                    da = _bbox_subset(da.to_dataset(name="soil_moisture"), args.bbox)[
                        "soil_moisture"
                    ]
            except RuntimeError as exc:
                # _slice_from_file, _read_source_units, _reduce_geolocation, and
                # _bbox_subset all raise RuntimeError; route them through _fail so a
                # mid-loop failure removes any partial store before exiting.
                _fail(f"Error: {exc}")
            ds_day = da.expand_dims(time=[np.datetime64(day_iso)]).to_dataset()

            _stamp_cf(ds_day, entry)

            # Clear per-variable encoding (codecs/chunks/dtype not part of the
            # envelope contract), then set the WRITE ENCODING for time
            # units/calendar and the soil_moisture _FillValue AFTER the clear so
            # the clear cannot drop them.
            for v in ds_day.variables:
                ds_day[v].encoding = {}
            ds_day["time"].encoding["units"] = _TIME_UNITS
            ds_day["time"].encoding["calendar"] = _TIME_CALENDAR
            ds_day["soil_moisture"].encoding["_FillValue"] = np.float64(np.nan)

            try:
                # Write-side CF decode check on the first slice (the schema is
                # identical for every appended day).
                if not store_created:
                    _cf_decode_check(ds_day)
                    if out.exists():
                        shutil.rmtree(out)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    # Mark the store as this run's BEFORE the write. Any
                    # pre-existing store was just removed above, so from here on
                    # `out` can only exist as this run's (possibly partial) output.
                    # Setting the flag first means a failure during the first
                    # mode="w" write — which may already have created a partial
                    # directory — is cleaned up by _fail, while a valid store from a
                    # previous successful run cannot be deleted (it is gone before
                    # the flag flips).
                    store_created = True
                    ds_day.to_zarr(out, mode="w", consolidated=True)
                else:
                    ds_day.to_zarr(out, mode="a", append_dim="time", consolidated=True)
            except Exception as exc:
                _fail(f"Error: failed to write the {day_iso} time step to {args.output}: {exc}")
            total_time += 1

    print(f"Wrote: {args.output} (time={total_time})", file=sys.stderr)


if __name__ == "__main__":
    main()
