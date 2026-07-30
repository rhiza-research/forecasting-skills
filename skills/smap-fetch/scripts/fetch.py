# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@cursor/simplify-weather-skill-decorator",
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
"""Fetch SMAP SPL3SMP_E soil moisture via Earthdata and write a weather-skills envelope Zarr."""

import re
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta

# cf_units and cf_xarray are used only inside weather-skills-core, which imports
# them lazily at write time (udunits_error / cf_axes_missing) — the final step,
# after every granule download. Importing them eagerly at module top turns that
# late import into a startup fail-fast probe: a missing dependency errors before
# any network work rather than only after every download has run. The F401 noqa
# marks these probe-only imports; removing either would drop the fail-fast
# guarantee.
import cf_units  # noqa: F401  (loaded lazily by core's udunits_error at write time)
import cf_xarray  # noqa: F401  (loaded lazily by core's cf_axes_missing at write time)
from weather_skills_core import DataError, SkillError, UsageError, weather_skill
from weather_skills_core.envelope import cf_axes_missing, stamp_cf_coords, udunits_error

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.9"

_SHORT_NAME = "SPL3SMP_E"
_FILL = -9999.0
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
# and the output envelope are:
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

# Single actionable, credential-free message raised on any Earthdata auth
# failure, so the wording stays identical regardless of which path raises it.
_AUTH_FAIL_MSG = (
    "Earthdata authentication failed; configure EARTHDATA_USERNAME/"
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

def _bbox_subset(ds, bbox):
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox."""
    north, west, south, east = bbox
    lat = ds["latitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    ds = ds.sel(latitude=lat_slice)
    if west > east:
        ds = ds.where((ds["longitude"] >= west) | (ds["longitude"] <= east), drop=True)
    else:
        ds = ds.sel(longitude=slice(west, east))
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        # Raise rather than exit: this runs inside the per-day loop, so the
        # streaming rollback can clean up any partial store first.
        raise RuntimeError(
            f"--bbox {north}/{west}/{south}/{east} selects no grid cells; check the extent and N/W/S/E order."
        )
    return ds

def _granule_date(granule) -> date:
    """Parse the acquisition date from a granule's data-file name."""
    for url in granule.data_links():
        m = _GRANULE_DATE_RE.search(url.rsplit("/", 1)[-1])
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007 -- date-only parse; a timezone is meaningless here
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
    import numpy as np

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
        # Raise rather than exit: this runs (via _slice_from_file) inside the
        # per-day loop, so the streaming rollback can clean up any partial
        # store first.
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
        # Raise rather than exit: this runs (via _slice_from_file) inside the
        # per-day loop, so the streaming rollback can clean up any partial
        # store first.
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
        # Raise rather than exit: this runs (via _slice_from_file) inside the
        # per-day loop, so the streaming rollback can clean up any partial
        # store first.
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
    import h5py
    import numpy as np
    import xarray as xr

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
    """Raise the single actionable, credential-free auth failure (exit 1).

    Raised before any write (`_login`, `_search`) and from the per-day loop
    alike; a mid-stream raise additionally triggers the streaming rollback,
    which removes the partial store before the message is printed.
    """
    raise DataError(_AUTH_FAIL_MSG)

def _login() -> None:
    """Authenticate to Earthdata, converting any failure into the actionable message.

    earthaccess.login() consumes the credentials itself; this wrapper never reads,
    prints, or checks a credential value. It tries the non-interactive strategies
    in order — `environment` (EARTHDATA_USERNAME/PASSWORD or EARTHDATA_TOKEN) then
    `netrc` (~/.netrc) — and deliberately never falls through to `interactive`,
    which would block on a tty prompt. If neither strategy authenticates (creds
    absent or rejected), raise the one-line actionable message (exit 1).
    """
    import earthaccess
    from earthaccess.exceptions import LoginAttemptFailure, LoginStrategyUnavailable

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
    import earthaccess

    try:
        return earthaccess.search_data(
            short_name=_SHORT_NAME,
            temporal=(start_iso, end_iso),
        )
    except SkillError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            _auth_fail()
        raise

def _download(granules, local_path: str):
    """Download granules, mapping a 401/403 to the auth message."""
    import earthaccess

    try:
        return earthaccess.download(granules, local_path=local_path)
    except SkillError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            _auth_fail()
        raise

def _stamp_cf(ds) -> None:
    """Stamp full CF-1.13 metadata onto the dataset in place.

    Global Conventions/title/source/institution/references/history; coordinate
    standard_name/units/axis on lat/lon/time; soil_moisture standard_name +
    verbatim source units + long_name + grid_mapping; a latitude_longitude
    grid_mapping container variable. Validates the data-var units against udunits.
    The provenance attrs (weather_skills_source/weather_skills_history) are
    stamped by the decorator on every write, after this runs. The time
    udunits/calendar and the soil_moisture _FillValue are NOT set here — they
    live in the WRITE ENCODING so the per-variable .encoding clear cannot drop
    them.
    """
    import xarray as xr

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
        history=f"{now_iso}: fetched via smap-fetch v{_SKILL_VERSION}",
    )

    stamp_cf_coords(ds)

    # Real udunits validation of the data-var units the way a CF-aware reader
    # would parse them; refuse to write rather than emit a false CF claim.
    units_exc = udunits_error(source_units, catch=(Exception,))
    if units_exc is not None:
        raise DataError(
            f"soil_moisture units {source_units!r} are not udunits-valid "
            f"({units_exc}); refusing to write a CF-noncompliant store."
        ) from units_exc
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
    missing = cf_axes_missing(ds)
    if missing:
        raise DataError(
            f"cf-xarray could not resolve axes {missing} from the stamped "
            "CF attrs; refusing to write a store downstream skills cannot decode."
        )

def _set_write_encoding(ds) -> None:
    """Controlled write encoding, applied after the decorator's encoding clear:
    the time units/calendar and the soil_moisture _FillValue."""
    import numpy as np

    ds["time"].encoding["units"] = _TIME_UNITS
    ds["time"].encoding["calendar"] = _TIME_CALENDAR
    ds["soil_moisture"].encoding["_FillValue"] = np.float64(np.nan)

@weather_skill(
    "smap-fetch",
    _SKILL_VERSION,
    outputs=["observations"]
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument(
            "--overpass",
            choices=["AM", "PM"],
            default="AM",
            help=(
                "Half-orbit overpass group to read (AM = 6am descending, PM = 6pm "
                "ascending). Default AM."
            ),
        )
def fetch(start_time, end_time, bbox, overpass, **kwargs):
    """Fetch SMAP SPL3SMP_E soil moisture via Earthdata and write a weather-skills envelope Zarr."""
    import numpy as np
    import xarray as xr

    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    requested_span = (end_time - start_time).days + 1

    print(f"Fetching SMAP {_SHORT_NAME} ({overpass}) {start_iso}..{end_iso}", file=sys.stderr)
    _login()
    results = _search(start_iso, end_iso)
    in_window = {}
    for r in results:
        d = _granule_date(r)
        if start_time <= d <= end_time:
            in_window.setdefault(d, r)
    if not in_window:
        raise DataError(f"no SMAP granule day within {start_iso}..{end_iso}.")
    print(f"Found {len(in_window)} granule day(s)", file=sys.stderr)

    present_days = sorted(in_window)
    requested_days = [start_time + timedelta(days=i) for i in range(requested_span)]
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

    day_datasets = []
    with tempfile.TemporaryDirectory(prefix="smap-fetch-") as td:
        for d in present_days:
            day_iso = d.isoformat()
            try:
                files = _download([in_window[d]], local_path=td)
            except SkillError:
                raise
            except Exception as exc:
                if _is_auth_error(exc):
                    raise DataError(_AUTH_FAIL_MSG) from exc
                raise DataError(
                    f"failed to download the SMAP granule for {day_iso}: {exc}"
                ) from exc
            if not files:
                raise DataError(f"download returned no local file for the {day_iso} granule.")
            try:
                da = _slice_from_file(files[0], overpass, day_iso)
                if bbox is not None:
                    da = _bbox_subset(da.to_dataset(name="soil_moisture"), bbox)[
                        "soil_moisture"
                    ]
            except RuntimeError as exc:
                raise DataError(str(exc)) from exc
            ds_day = da.expand_dims(time=[np.datetime64(day_iso)]).to_dataset()
            _stamp_cf(ds_day)
            day_datasets.append(ds_day)

    ds = xr.concat(day_datasets, dim="time")
    ds.attrs["weather_skills_source"] = "smap"
    _cf_decode_check(ds)
    _set_write_encoding(ds)
    return ds

if __name__ == "__main__":
    fetch()
