# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "xarray",
#   "zarr",
#   "numpy",
#   "netCDF4",
#   "cf_xarray",
#   "pint-xarray>=0.6",
#   "cftime",
# ]
# ///
"""Fetch NOAA OISST v2.1 daily sea-surface temperature from NOAA PSL OPeNDAP and write a weather-skills standard dataset Zarr."""

import sys
from datetime import UTC, datetime

import cf_xarray  # noqa: F401  (fail-fast probe; core loads it lazily at write time)
from weather_skills_core import DataError, SkillError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_coords
from weather_skills_core.standard_utils import (
    apply_write_encoding,
    bbox_subset,
    normalize_longitude,
    np_to_date,
    verify_cf_decode,
)
from weather_skills_core.units import convert_dataarray, stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_OPENDAP_URL = (
    "https://psl.noaa.gov/thredds/dodsC/Datasets/noaa.oisst.v2.highres/sst.day.mean.{year}.nc"
)

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

_SST_STANDARD_NAME = "sea_surface_temperature"
_SST_LONG_NAME = "Daily Sea Surface Temperature"

# Source attrs that describe the pre-subset global extent or contradict NaN fill.
_STALE_RANGE_ATTRS = (
    "actual_range",
    "valid_range",
    "_ChunkSizes",
    "missing_value",
    "valid_min",
    "valid_max",
)

_TIME_UNITS = "days since 1970-01-01 00:00:00"
_TIME_CALENDAR = "standard"


def _strip_dangling_bounds(ds):
    """Remove a `bounds` attr when the named bounds variable is absent."""
    present = set(ds.variables)
    for name in ds.coords:
        bnds = ds[name].attrs.get("bounds")
        if bnds is not None and bnds not in present:
            del ds[name].attrs["bounds"]
    return ds


def _stamp_cf(ds):
    """Stamp CF-1.13 attrs; convert sst to degree_Celsius."""
    import cf_xarray.units  # noqa: F401
    from pint import application_registry as ureg

    ds.attrs.update(_CF_GLOBAL_ATTRS)
    stamp_cf_coords(
        ds, long_names={"latitude": "Latitude", "longitude": "Longitude", "time": "Time"}
    )

    src_units = ds["sst"].attrs.get("units")
    try:
        unit = ureg.Unit(src_units)
        valid = bool(src_units) and unit.is_compatible_with(ureg.Unit("K"))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise DataError(
            f"source OISST sst units {src_units!r} are not a temperature "
            "unit convertible to K; refusing to stamp CF "
            f"standard_name={_SST_STANDARD_NAME!r}."
        )
    converted, _ = convert_dataarray(ds["sst"], "degree_Celsius")
    ds["sst"] = converted
    ds["sst"].attrs["standard_name"] = _SST_STANDARD_NAME
    ds["sst"].attrs["long_name"] = _SST_LONG_NAME
    ds["sst"].attrs["units"] = "degree_Celsius"

    for name in ("sst", "latitude", "longitude", "time"):
        if name in ds.variables:
            for attr in _STALE_RANGE_ATTRS:
                ds[name].attrs.pop(attr, None)

    return _strip_dangling_bounds(ds)


def _is_availability_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        m in text for m in ("not found", "no such file", "404", "does not exist", "file not found")
    )


def _is_transport_failure(exc: Exception) -> bool:
    if _is_availability_failure(exc):
        return False
    text = str(exc).lower()
    return any(
        m in text
        for m in ("dap failure", "dap2", "dap", "curl", "connection", "timed out", "timeout")
    )


@weather_skill(
    name="oisst-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    default=None,
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest available YYYY-MM-DD (or none) on stdout and exit. "
        "Does not download fields. Optional IDENT selects a product "
        "(dataset id, IMERG late/final, …)."
    ),
)
def fetch(start_time, end_time, bbox, **kwargs):
    """Fetch NOAA OISST v2.1 daily sea-surface temperature from NOAA PSL OPeNDAP and write a weather-skills standard dataset Zarr."""
    if kwargs.get("probe_latest") is not None:
        import numpy as np
        import xarray as xr

        year = datetime.now(UTC).date().year
        last_exc = None
        for y in (year, year - 1):
            try:
                ds = xr.open_dataset(_OPENDAP_URL.format(year=y))
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            latest = np_to_date(np.max(ds["time"].values))
            ds.close()
            print(latest.isoformat())
            return
        raise DataError(
            "could not open a current or previous-year OISST file to probe latest time"
            + (f" ({last_exc})" if last_exc is not None else "")
        )

    import numpy as np
    import xarray as xr

    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    time_slice = slice(np.datetime64(f"{start_iso}T00:00"), np.datetime64(f"{end_iso}T23:59"))
    years = list(range(start_time.year, end_time.year + 1))
    bbox_label = f"{bbox[0]}/{bbox[1]}/{bbox[2]}/{bbox[3]}" if bbox is not None else "global"
    print(
        f"Fetching oisst {start_iso}..{end_iso} (years {years[0]}..{years[-1]})",
        file=sys.stderr,
    )

    pieces = []
    for year in years:
        try:
            # No dask chunking: chunked OPeNDAP reads were observed to write zeros.
            dy = xr.open_dataset(_OPENDAP_URL.format(year=year))
        except Exception as exc:
            raise DataError(
                f"could not open the OISST file for year {year} ({exc}). The year may be "
                "outside the served range (1981-09 to present), or NOAA PSL's OPeNDAP server is "
                "unreachable — check the date range."
            ) from exc
        try:
            with dy:
                piece = dy[["sst"]].rename({"lat": "latitude", "lon": "longitude"})
                piece = normalize_longitude(piece)
                piece = piece.sel(time=time_slice)
                if bbox is not None:
                    piece = bbox_subset(piece, bbox, lat_dim="latitude", lon_dim="longitude")
                piece = piece.load()
        except SkillError:
            raise
        except Exception as exc:
            if _is_availability_failure(exc):
                raise UsageError(
                    f"could not read the OISST file for year {year} ({exc}). The year may be "
                    "outside the served range (1981-09 to present), or that year's file is not yet "
                    "available — check the date range and use an absolute --start-time/--end-time in the "
                    "served range."
                ) from exc
            if _is_transport_failure(exc):
                raise UsageError(
                    f"OISST OPeNDAP rejected the data transfer for {start_iso}..{end_iso} "
                    f"bbox {bbox_label} (year {year}): {exc}. OISST is served "
                    "over NOAA PSL OPeNDAP, which limits request size; this request is too large. "
                    "Reduce --bbox and/or shorten the date range."
                ) from exc
            raise UsageError(f"unexpected failure reading OISST year {year}: {exc}.") from exc
        if piece.sizes.get("time", 0) == 0:
            continue
        _stamp_cf(piece)
        pieces.append(piece)

    if not pieces:
        raise DataError(f"OISST has no data in {start_iso}..{end_iso}.")

    ds = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
    ds.attrs["weather_skills_source"] = "oisst"
    apply_write_encoding(
        ds,
        time_units=_TIME_UNITS,
        time_calendar=_TIME_CALENDAR,
        fills={"sst": np.float32("nan")},
    )
    verify_cf_decode(ds)
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()
