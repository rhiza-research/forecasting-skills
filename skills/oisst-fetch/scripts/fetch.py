# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "xarray",
#   "zarr",
#   "numpy",
#   "netCDF4",
#   "cf_xarray",
#   "cf_units",
#   "cftime",
# ]
# ///
"""Fetch NOAA OISST v2.1 daily sea-surface temperature from NOAA PSL OPeNDAP and write a weather-skills envelope Zarr."""

import sys
from datetime import UTC, date, datetime

# cf_xarray is used only inside weather-skills-core, which imports it lazily at
# write time (cf_axes_missing, in the post-write decode check) — the final step,
# after every per-year OPeNDAP fetch. Importing it eagerly at module top turns
# that late import into a startup fail-fast probe: a missing dependency errors
# before any network work rather than only after every fetch has run. The
# F401 suppression below marks the probe-only import; removing it would drop the
# fail-fast guarantee. (cf_units is imported where it is used directly, in
# _stamp_cf.)
import cf_xarray  # noqa: F401  (loaded lazily by core's cf_axes_missing at write time)
from weather_skills_core import DataError, SkillError, UsageError, weather_skill
from weather_skills_core.dates import np_to_date
from weather_skills_core.envelope import cf_axes_missing, normalize_longitude, stamp_cf_coords
from weather_skills_core.provenance import make_completeness_probe

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.7"

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
# the write-encoding hook (not left to xarray defaults) so the output's time axis
# decodes deterministically.
_TIME_UNITS = "days since 1970-01-01 00:00:00"
_TIME_CALENDAR = "standard"


# Cache-hit completeness probe: `sst` must be present and its last time slice
# must decode, and the `time` coord must be fully valued and strictly
# increasing. The write path removes a partial store on failure; the probe is
# the backstop for stores left partial by other means (a killed process, a
# full disk mid-append).
_store_is_complete = make_completeness_probe("sst", check_time="time")


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
    import cf_units

    ds.attrs.update(_CF_GLOBAL_ATTRS)

    stamp_cf_coords(
        ds, long_names={"latitude": "Latitude", "longitude": "Longitude", "time": "Time"}
    )

    src_units = ds["sst"].attrs.get("units")
    try:
        unit = cf_units.Unit(src_units)
        valid = (not unit.is_no_unit()) and unit.is_convertible(cf_units.Unit("K"))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise DataError(
            f"source OISST sst units {src_units!r} are not a udunits temperature "
            "unit convertible to K; refusing to stamp CF "
            f"standard_name={_SST_STANDARD_NAME!r} under an invalid units claim. The "
            "source product changed its units; this fetcher must be revisited."
        )
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


def _cf_decode_check(out) -> None:
    """Reopen the written store and confirm cf-xarray resolves the X/Y/T axes.

    A write that does not decode as CF (coord attrs missing/wrong, axes
    unresolvable) is a defect under the full-CF contract; surface it rather than
    ship a store that only looks compliant. Runs as the post_write hook, after
    the streamed write completes and before the Wrote line.
    """
    import xarray as xr

    try:
        with xr.open_zarr(out, consolidated=True, decode_cf=True) as ds:
            missing = cf_axes_missing(ds)
    except Exception as exc:
        raise DataError(
            f"wrote {out} but cf-xarray could not decode it ({exc}); the output "
            "is not CF-compliant."
        ) from exc
    if missing:
        raise DataError(
            f"wrote {out} but cf-xarray did not resolve axes {missing} "
            "(expected X/Y/T); the output is not CF-compliant."
        )


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
        raise DataError("--bbox selects no grid cells; check the extent and N/W/S/E order.")
    return ds


def _open_year(year: int):
    """Open one OISST year's OPeNDAP dataset.

    No dask chunking: the netCDF4 OPeNDAP backend's own lazy indexing reads only
    the cells touched by the later .sel()/.load(), and a chunked (dask) read over
    this backend was observed to silently write zeros instead of the real values.
    """
    import xarray as xr

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


def _latest(args) -> date:
    """`latest` resolver for OISST: newest day in the current-year OPeNDAP file.

    The newest available day lives in the current-year file; fall back to the
    previous year early in January before the new year's file appears. Each
    open is classified: a transport failure (server unreachable) is distinct
    from a genuine absence of the year file.
    """
    today = datetime.now(UTC).date()
    transport_err = None
    for year in (today.year, today.year - 1):
        try:
            with _open_year(year) as dy:
                return np_to_date(dy["time"].values.max())
        except Exception as exc:  # noqa: BLE001 -- try the previous year, else classify below
            # Only a true transport marker (not a not-found/availability error)
            # routes to the "server unreachable" message; an absent year file
            # (early January, before the new year's file appears) falls through
            # to the availability guidance below.
            if _is_transport_failure(exc):
                transport_err = exc
            continue
    if transport_err is not None:
        raise DataError(
            "could not resolve 'latest' — NOAA PSL's OPeNDAP server looks "
            f"unreachable (transport failure: {transport_err}). This is not a "
            "data-availability problem; check connectivity and retry."
        )
    raise DataError(
        "could not resolve 'latest' — neither the current nor the "
        f"previous year file ({today.year}, {today.year - 1}) was available on "
        "NOAA PSL OPeNDAP. Use an absolute --start/--end in the served range "
        "(1981-09 to present) instead."
    )


def _set_write_encoding(ds) -> None:
    """Controlled CF write encoding, applied after the decorator's encoding clear:
    explicit time units/calendar and an explicit NaN _FillValue for sst land cells."""
    import numpy as np

    ds["time"].encoding["units"] = _TIME_UNITS
    ds["time"].encoding["calendar"] = _TIME_CALENDAR
    ds["sst"].encoding["_FillValue"] = np.float32("nan")


@weather_skill(
    "oisst-fetch",
    _SKILL_VERSION,
    output_type="gridded",
    source="oisst",
    start_time=True,
    end_time=True,
    bbox="optional",
    latest_resolver=_latest,
    completeness_probe=_store_is_complete,
    write_encoding=_set_write_encoding,
    post_write=_cf_decode_check,
    streaming=True,
    cache_hit_label="fetch",
)
def fetch(start_time, end_time, bbox, context):
    """Fetch NOAA OISST v2.1 daily sea-surface temperature from NOAA PSL OPeNDAP and write a weather-skills envelope Zarr."""
    import numpy as np

    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()
    time_slice = slice(np.datetime64(f"{start_iso}T00:00"), np.datetime64(f"{end_iso}T23:59"))
    years = list(range(start_time.year, end_time.year + 1))
    print(f"Fetching oisst {start_iso}..{end_iso} (years {years[0]}..{years[-1]})", file=sys.stderr)

    # Stream one year at a time: subset each year to the bbox + window, pull just
    # that slice into memory (eager per-year load avoids the dask-over-OPeNDAP path
    # that silently wrote zeros), then yield it for the decorator to write/append
    # before moving to the next year. Peak resident memory is bounded to a single
    # year's selection rather than the whole multi-year window. The decorator
    # re-stamps weather_skills_source/weather_skills_history on every append (a
    # to_zarr append rewrites the root group attrs from the appended dataset; the
    # entry is identical each time, so the final stamp is stable) and removes a
    # partial store on any mid-stream failure, so a later identical run cannot
    # falsely accept a truncated store as a cache hit.
    wrote_any = False
    for year in years:
        try:
            dy = _open_year(year)
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
                # Narrow the TIME axis first, before any spatial selection. The
                # antimeridian branch of _bbox_subset uses an eager `.where(drop=True)`,
                # so doing it before the time subset would materialize the full year's
                # longitude wings across the entire year's time axis; subsetting time
                # first keeps that eager spatial read bounded to the requested window.
                piece = piece.sel(time=time_slice)
                if bbox is not None:
                    piece = _bbox_subset(piece, *bbox)
                piece = piece.load()
        except SkillError:
            # _bbox_subset's empty-selection DataError already says what failed;
            # propagate it past the transfer-failure classifier below.
            raise
        except Exception as exc:
            if _is_availability_failure(exc):
                raise UsageError(
                    f"could not read the OISST file for year {year} ({exc}). The year may be "
                    "outside the served range (1981-09 to present), or that year's file is not yet "
                    "available — check the date range and use an absolute --start/--end in the "
                    "served range."
                ) from exc
            if _is_transport_failure(exc):
                raise UsageError(
                    f"OISST OPeNDAP rejected the data transfer for {start_iso}..{end_iso} "
                    f"bbox {context.args.bbox or 'global'} (year {year}): {exc}. OISST is served "
                    "over NOAA PSL OPeNDAP, which limits request size; this request is too large. "
                    "Reduce --bbox and/or shorten the date range. This is not a credentials or "
                    "data-availability problem — retrying the same request will not help."
                ) from exc
            raise UsageError(f"unexpected failure reading OISST year {year}: {exc}.") from exc
        if piece.sizes.get("time", 0) == 0:
            continue
        _stamp_cf(piece)
        wrote_any = True
        yield piece

    if not wrote_any:
        raise DataError(f"OISST has no data in {start_iso}..{end_iso}.")


if __name__ == "__main__":
    fetch()
