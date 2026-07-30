# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core",
#   "xarray",
#   "zarr",
#   "gcsfs",
#   "numpy",
#   "cf_xarray",
#   "cf_units",
#   "cftime",
# ]
#
# [tool.uv.sources]
# weather-skills-core = { path = "../../../../weather-skills-core", editable = true }
# ///
"""Fetch ARCO-ERA5 reanalysis from the public Google Cloud Zarr and write a WeatherSkills standard dataset."""

import re
import sys
from datetime import UTC, date, datetime

from weather_skills_core import DataError, EntryOverride, Types, UsageError, weather_skill
from weather_skills_core.dataset import bbox_subset, normalize_longitude
from weather_skills_core.dates import np_to_date

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.9"

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
#   2. Longitude normalization (weather_skills_core's `normalize_longitude`):
#      ERA5's native 0..360 longitude is mapped onto [-180, 180) and the axis
#      re-sorted ascending so negative-west/east bboxes select correctly.
#      Coordinate relabel of the same grid; sample values are unchanged.
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

# Strict absolute-date shape, used to validate the store's valid-time marker
# attrs before trusting them as the `latest` data edge.
_ABS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_STATE: dict = {}


def _open_arco():
    """Open the ARCO store lazily (metadata only), once per process."""
    if "ds" not in _STATE:
        import xarray as xr

        try:
            _STATE["ds"] = xr.open_zarr(_ARCO_STORE, storage_options=_STORAGE_OPTIONS, chunks=None)
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"could not open the ARCO-ERA5 store at {_ARCO_STORE} "
                f"({type(exc).__name__}: {exc}). Check network access to Google Cloud "
                "Storage; the store is read anonymously, so no credentials are needed."
            ) from None
    return _STATE["ds"]


def _latest() -> date:
    """Newest date with data from store marker attrs (not the pre-allocated time max)."""
    ds = _open_arco()
    for attr in ("valid_time_stop_era5t", "valid_time_stop"):
        raw = ds.attrs.get(attr)
        if raw and _ABS_DATE_RE.match(str(raw).strip()):
            try:
                return date.fromisoformat(str(raw).strip())
            except ValueError:
                pass
    return np_to_date(ds["time"].values.max())


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


def _resolve(d):
    return _latest() if d == "latest" else d


@weather_skill(
    name="arco-era5-fetch",
    version=_SKILL_VERSION,
    outputs=[Types.GRIDDED],
    required_args=("start_time", "end_time", "variable"),
    optional_args=("bbox",),
    exclude_args=("workers",),
    check_cache=True,
    source="arco-era5",
)
@weather_skill.argument("--workers", type=int, default=1, help="Unused; reserved for future parallelism.")
def fetch(start_time, end_time, variable, bbox, workers):
    """Fetch ARCO-ERA5 reanalysis and write a WeatherSkills standard dataset."""
    import numpy as np

    start_time, end_time = _resolve(start_time), _resolve(end_time)
    start_iso, end_iso = start_time.isoformat(), end_time.isoformat()
    ds = _open_arco()

    missing = [v for v in variable if v not in ds.data_vars]
    if missing:
        raise UsageError(
            f"variable(s) not in ARCO-ERA5: {', '.join(missing)}.\n"
            f"Available: {', '.join(sorted(ds.data_vars))}"
        )
    ds = ds[variable]
    ds = normalize_longitude(ds)
    if bbox:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")
    ds = ds.sel(time=slice(np.datetime64(f"{start_iso}T00:00"), np.datetime64(f"{end_iso}T23:59")))
    if ds.sizes.get("time", 0) == 0:
        raise DataError(f"ARCO-ERA5 has no data in {start_iso}..{end_iso}.")

    print(f"Fetching arco-era5 {start_iso}..{end_iso}", file=sys.stderr)
    ds.attrs.clear()
    ds.attrs.update(_global_attrs(start_iso, end_iso))
    _stamp_coord_attrs(ds)
    try:
        _stamp_data_var_attrs(ds)
    except ValueError as exc:
        raise DataError(str(exc)) from None

    import cf_xarray  # noqa: F401

    try:
        axes = ds.cf.axes
        for required in ("X", "Y", "T"):
            if required not in axes:
                raise ValueError(f"CF axis {required} did not resolve from coord attrs")
        if "level" in ds.coords and "Z" not in ds.cf.axes and "vertical" not in ds.cf.coordinates:
            raise ValueError("level present but did not resolve as a CF vertical coordinate")
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            f"the output failed the CF decode check before writing ({exc}). "
            "This is a bug in the fetcher's CF stamping, not a data problem."
        ) from None

    if "time" in ds.coords:
        ds["time"].encoding = {
            "units": f"hours since {start_iso} 00:00:00",
            "calendar": "proleptic_gregorian",
        }

    try:
        ds = ds.compute()
    except MemoryError:
        raise DataError(
            "ran out of memory materializing the selection. "
            "Narrow it with -v, --bbox, or a shorter window."
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            f"failed while reading from the ARCO-ERA5 store ({type(exc).__name__}: {exc})."
        ) from None

    return ds, EntryOverride(args={"start_time": start_iso, "end_time": end_iso})


if __name__ == "__main__":
    fetch()
