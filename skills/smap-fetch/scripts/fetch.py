# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "earthaccess",
#   "h5py",
#   "xarray",
#   "zarr",
#   "numpy",
#   "cf_xarray",
#   "pint-xarray>=0.6",
#   "cftime",
# ]
# ///
"""Fetch SMAP SPL3SMP_E soil moisture via Earthdata and write a weather-skills standard dataset Zarr."""

import re
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta

import cf_xarray.units  # noqa: F401  (fail-fast probe; configures pint CF registry)
import cf_xarray  # noqa: F401  (fail-fast probe)
from pathlib import Path

from weather_skills_core import DataError, SkillError, weather_skill
from weather_skills_core.cf import stamp_cf_coords, udunits_error
from weather_skills_core.probe import PROBE_LATEST_KWARGS
from weather_skills_core.standard_utils import (
    apply_write_encoding,
    bbox_subset,
    verify_cf_decode,
)
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_SHORT_NAME = "SPL3SMP_E"
_FILL = -9999.0
_GRANULE_DATE_RE = re.compile(r"_(\d{8})_")
_SM_STANDARD_NAME = "volume_fraction_of_condensed_water_in_soil"

_CF_CONVENTIONS = "CF-1.13"
_CF_TITLE = "SMAP Enhanced L3 Radiometer Global Daily Soil Moisture"
_CF_SOURCE = "SMAP SPL3SMP_E (Enhanced L3 Radiometer 9 km EASE-Grid 2.0 soil moisture)"
_CF_INSTITUTION = "NASA National Snow and Ice Data Center DAAC"
_CF_REFERENCES = "https://nsidc.org/data/spl3smp_e"

_AUTH_FAIL_MSG = (
    "Earthdata authentication failed; configure EARTHDATA_USERNAME/"
    "EARTHDATA_PASSWORD or a urs.earthdata.nasa.gov entry in ~/.netrc, then retry."
)

_TIME_UNITS = "days since 1970-01-01 00:00:00"
_TIME_CALENDAR = "proleptic_gregorian"
_GRID_MAPPING_NAME = "latitude_longitude"


def _granule_date(granule) -> date:
    for url in granule.data_links():
        m = _GRANULE_DATE_RE.search(url.rsplit("/", 1)[-1])
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007
    raise ValueError("could not parse a YYYYMMDD date from the granule file name")


def _reduce_geolocation(lat2d, lon2d, day_iso: str) -> tuple:
    """Reduce 2-D EASE-Grid lat/lon to 1-D vectors; fail if not row/col-constant."""
    import numpy as np

    with np.errstate(invalid="ignore"):
        lat_row_spread = np.nanmax(lat2d, axis=1) - np.nanmin(lat2d, axis=1)
        lon_col_spread = np.nanmax(lon2d, axis=0) - np.nanmin(lon2d, axis=0)
    tol = 1e-3
    max_lat_spread = float(np.nanmax(lat_row_spread)) if lat_row_spread.size else 0.0
    max_lon_spread = float(np.nanmax(lon_col_spread)) if lon_col_spread.size else 0.0
    if max_lat_spread > tol or max_lon_spread > tol:
        raise RuntimeError(
            f"granule for {day_iso} is not row-constant-lat / col-constant-lon "
            f"within {tol} deg (max per-row lat spread {max_lat_spread:.5f}, max "
            f"per-col lon spread {max_lon_spread:.5f}); cannot reduce the 2-D "
            "EASE-Grid geolocation to 1-D coordinate vectors without distorting it."
        )
    lat1d = np.nanmean(lat2d, axis=1)
    lon1d = np.nanmean(lon2d, axis=0)
    if not (np.isfinite(lat1d).all() and np.isfinite(lon1d).all()):
        raise RuntimeError(
            f"granule for {day_iso} has an all-fill geolocation row/column; "
            "the reduced 1-D latitude/longitude contains non-finite values and "
            "cannot be used as coordinate vectors."
        )
    return lat1d, lon1d


def _slice_from_file(path: str, group: str, day_iso: str):
    """Read one SPL3SMP_E granule into a (latitude, longitude) DataArray."""
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
            raw_units = grp["soil_moisture"].attrs.get("units")
            if raw_units is None:
                raise RuntimeError(
                    f"granule for {day_iso} has no units attribute on soil_moisture; "
                    "cannot pass source units through verbatim and will not fabricate one."
                )
            if isinstance(raw_units, bytes):
                raw_units = raw_units.decode("utf-8", "replace")
            source_units = str(raw_units).strip()
            sm = grp["soil_moisture"][:].astype("float64")
            lat2d = grp["latitude"][:].astype("float64")
            lon2d = grp["longitude"][:].astype("float64")
    except KeyError as exc:
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

    return xr.DataArray(
        sm,
        dims=("latitude", "longitude"),
        coords={"latitude": lat1d, "longitude": lon1d},
        name="soil_moisture",
        attrs={"units": source_units},
    )


def _is_auth_error(exc: Exception) -> bool:
    for status in (
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
    ):
        if status in (401, 403):
            return True
    msg = str(exc).lower()
    return "401 unauthorized" in msg or "403 forbidden" in msg


def _login() -> None:
    """Authenticate via environment then netrc; never falls through to interactive."""
    import earthaccess
    from earthaccess.exceptions import LoginAttemptFailure, LoginStrategyUnavailable

    for strategy in ("environment", "netrc"):
        try:
            auth = earthaccess.login(strategy=strategy)
        except LoginStrategyUnavailable:
            continue
        except LoginAttemptFailure:
            raise DataError(_AUTH_FAIL_MSG) from None
        except Exception as exc:
            if _is_auth_error(exc):
                raise DataError(_AUTH_FAIL_MSG) from None
            raise
        if getattr(auth, "authenticated", False):
            return
    raise DataError(_AUTH_FAIL_MSG)


def _earthaccess_call(fn, *args, **kwargs):
    """Call an earthaccess function; map 401/403 to the auth message."""
    try:
        return fn(*args, **kwargs)
    except SkillError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise DataError(_AUTH_FAIL_MSG) from None
        raise


def _stamp_cf(ds) -> None:
    import xarray as xr

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
    ds[_GRID_MAPPING_NAME] = xr.DataArray(0, attrs={})
    ds[_GRID_MAPPING_NAME].attrs.update(
        grid_mapping_name="latitude_longitude",
        longitude_of_prime_meridian=0.0,
        semi_major_axis=6378137.0,
        inverse_flattening=298.257223563,
    )


@weather_skill(
    name="smap-fetch",
    version=_SKILL_VERSION,
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
@weather_skill.argument("--probe-latest", **PROBE_LATEST_KWARGS)
def fetch(start_time, end_time, bbox, overpass, **kwargs):
    """Fetch SMAP SPL3SMP_E soil moisture via Earthdata and write a weather-skills standard dataset Zarr."""
    import earthaccess
    import numpy as np
    import xarray as xr

    if kwargs.get("probe_latest") is not None:
        end = datetime.now(UTC).date()
        start = end - timedelta(days=21)
        _login()
        results = _earthaccess_call(
            earthaccess.search_data,
            short_name=_SHORT_NAME,
            temporal=(start.isoformat(), end.isoformat()),
        )
        days = []
        for granule in results:
            try:
                days.append(_granule_date(granule))
            except ValueError:
                continue
        if not days:
            raise DataError(f"no SMAP granules in {start.isoformat()}..{end.isoformat()}")
        print(max(days).isoformat())
        return

    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    requested_span = (end_time - start_time).days + 1

    print(f"Fetching SMAP {_SHORT_NAME} ({overpass}) {start_iso}..{end_iso}", file=sys.stderr)
    _login()
    results = _earthaccess_call(
        earthaccess.search_data,
        short_name=_SHORT_NAME,
        temporal=(start_iso, end_iso),
    )
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
                files = _earthaccess_call(
                    earthaccess.download, [in_window[d]], local_path=td
                )
            except SkillError:
                raise
            except Exception as exc:
                raise DataError(
                    f"failed to download the SMAP granule for {day_iso}: {exc}"
                ) from exc
            if not files:
                raise DataError(f"download returned no local file for the {day_iso} granule.")
            try:
                da = _slice_from_file(files[0], overpass, day_iso)
                if bbox is not None:
                    da = bbox_subset(
                        da.to_dataset(name="soil_moisture"),
                        bbox,
                        lat_dim="latitude",
                        lon_dim="longitude",
                    )["soil_moisture"]
            except RuntimeError as exc:
                raise DataError(str(exc)) from exc
            ds_day = da.expand_dims(time=[np.datetime64(day_iso)]).to_dataset()
            _stamp_cf(ds_day)
            day_datasets.append(ds_day)

    ds = xr.concat(day_datasets, dim="time")
    ds.attrs["weather_skills_source"] = "smap"
    verify_cf_decode(ds)
    apply_write_encoding(
        ds,
        time_units=_TIME_UNITS,
        time_calendar=_TIME_CALENDAR,
        fills={"soil_moisture": np.float64(np.nan)},
    )
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()
