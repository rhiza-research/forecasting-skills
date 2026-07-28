# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
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

import re
import sys
from datetime import UTC, date, datetime

from weather_skills_core import DataError, UsageError, WroteSummary, types, weather_skill
from weather_skills_core.dates import np_to_date
from weather_skills_core.envelope import normalize_longitude
from weather_skills_core.provenance import make_completeness_probe

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


def _open_arco(state):
    """Open the ARCO store lazily (metadata only), at most once per run.

    ``state`` is the run-scoped ``RunContext.state`` dict, shared by the
    `latest` resolver and the fetch body so the store is opened at most once
    per run. Lazy open with chunks=None: reads only metadata, so `latest`
    resolution runs before any array bytes are pulled, and xarray's lazy
    indexing prunes to the bbox/time/variable selection before reading — so
    only that selection is materialized. (A dask-backed open forces the
    longitude normalization sort to pull the whole global grid per step,
    which is far slower for a bbox request with no memory benefit; the
    selection itself is the bound.) Store-open failures (network, bad path,
    gcsfs error) surface as a one-line actionable message rather than a raw
    traceback.
    """
    if "ds" not in state:
        import xarray as xr

        try:
            state["ds"] = xr.open_zarr(_ARCO_STORE, storage_options=_STORAGE_OPTIONS, chunks=None)
        except Exception as exc:  # noqa: BLE001
            raise DataError(
                f"could not open the ARCO-ERA5 store at {_ARCO_STORE} "
                f"({type(exc).__name__}: {exc}). Check network access to Google Cloud "
                "Storage; the store is read anonymously, so no credentials are needed."
            ) from None
    return state["ds"]


def _latest(args, context) -> date:
    """Newest date that actually has data, from the store's marker attrs.

    The store's `time` coordinate is pre-allocated far into the future (empty
    placeholder slots out to ~2050), so its max is not the data edge. The real
    extent is published in the store's global attrs: `valid_time_stop_era5t`
    marks the near-real-time (ERA5T) edge and `valid_time_stop` the
    finalized-ERA5 edge. Both are inclusive (data exists through that date);
    the near-real-time edge is preferred. Fall back to the time-coord max only
    if neither attr is present or parseable. The decorator memoizes this, so
    it is computed at most once and only when a token references `latest`.
    These marker attrs are trusted as the data edge and are not cross-checked
    against the actually-filled `time` slots, so in the rare case the store
    publishes a marker ahead of its written data, a `latest`-anchored request
    resolves to a date with no time steps and exits with the existing clean
    "no data in <start>..<end>" error rather than silently returning wrong
    data.
    """
    ds = _open_arco(context.state)
    for attr in ("valid_time_stop_era5t", "valid_time_stop"):
        raw = ds.attrs.get(attr)
        if raw and _ABS_DATE_RE.match(str(raw).strip()):
            try:
                return date.fromisoformat(str(raw).strip())
            except ValueError:
                pass
    return np_to_date(ds["time"].values.max())


# Cache-hit completeness probe, keyed to the requested `--variable` list (or
# every data variable already in the store when the list is omitted): each
# probed variable must be present and its corner cell must decode, so a store
# whose `weather_skills_history` attr survived an interrupted write is a cache
# miss that forces a re-fetch rather than a truncated output handed to the
# caller.
_store_is_complete = make_completeness_probe(lambda context: context.args.variable)


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


def _set_time_encoding(ds, context) -> None:
    """Controlled time write encoding, applied after the decorator's encoding clear.

    The time coord's CF units + calendar are set explicitly so the on-disk
    time axis is self-describing per CF; the reference date is the resolved
    window start, read off the run context.
    """
    if "time" in ds.coords:
        ds["time"].encoding = {
            "units": f"hours since {context.start_time.isoformat()} 00:00:00",
            "calendar": "proleptic_gregorian",
        }


@weather_skill(
    "arco-era5-fetch",
    _SKILL_VERSION,
    output_type=types.GRIDDED,
    source="arco-era5",
    start_time=True,
    end_time=True,
    bbox=types.OPTIONAL,
    variable={
        "mode": types.REPEAT,
        "help": "Restrict to this data variable. Repeat once per variable; omit for all (large).",
    },
    latest_resolver=_latest,
    completeness_probe=_store_is_complete,
    write_encoding=_set_time_encoding,
    cache_hit_label="fetch",
)
def fetch(start_time, end_time, bbox, variable, context):
    """Fetch ARCO-ERA5 reanalysis from the public Google Cloud Zarr and write a weather-skills envelope Zarr."""
    import numpy as np
    from weather_skills_core.envelope import bbox_subset

    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()

    ds = _open_arco(context.state)

    # Selection: variable -> bbox -> time. Each step prunes before the eager load.
    if variable:
        missing = [v for v in variable if v not in ds.data_vars]
        if missing:
            raise UsageError(
                f"variable(s) not in ARCO-ERA5: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(ds.data_vars))}"
            )
        ds = ds[variable]
    else:
        print(
            "Note: no --variable given; selecting all data variables (large). Pass -v to restrict.",
            file=sys.stderr,
        )

    ds = normalize_longitude(ds)
    if bbox:
        ds = bbox_subset(ds, bbox, lat_dim="latitude", lon_dim="longitude")

    # Inclusive whole-day window over the hourly time axis.
    ds = ds.sel(time=slice(np.datetime64(f"{start_iso}T00:00"), np.datetime64(f"{end_iso}T23:59")))
    if ds.sizes.get("time", 0) == 0:
        raise DataError(f"ARCO-ERA5 has no data in {start_iso}..{end_iso}.")

    print(
        f"Fetching arco-era5 {start_iso}..{end_iso}",
        file=sys.stderr,
    )

    ds.attrs.clear()
    ds.attrs.update(_global_attrs(start_iso, end_iso))
    _stamp_coord_attrs(ds)
    # Validate and stamp every data variable's CF attrs. A variable whose final
    # `units` cannot be made udunits-valid (missing, or non-parseable and not in
    # the fixup map) fails here rather than being written under a false CF claim.
    try:
        _stamp_data_var_attrs(ds)
    except ValueError as exc:
        raise DataError(str(exc)) from None

    # Write-side CF decode check: confirm every axis resolves from the stamped
    # coord attrs with cf_xarray's machinery. This catches a coord attr
    # regression before the store is written rather than at read time.
    import cf_xarray  # noqa: F401 — registers the .cf accessor

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

    # Materialize the pruned selection eagerly so read failures surface here
    # with an actionable message rather than as a raw traceback from the write.
    try:
        ds = ds.compute()
    except MemoryError:
        # Reactive backstop only: materializing loads the whole pruned
        # selection into host memory at once, so a very large selection can
        # exhaust memory. This handler is best-effort — under Linux memory
        # overcommit an OOM typically arrives as a SIGKILL (the process is
        # killed outright, printing only "Killed"), so a catchable MemoryError
        # is not guaranteed. When it is catchable, surface it actionably so a
        # caller narrows the request rather than blindly retrying the same
        # dead call.
        raise DataError(
            "ran out of memory materializing the selection. "
            "Narrow it with -v, --bbox, or a shorter window."
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            f"failed while reading from the ARCO-ERA5 store ({type(exc).__name__}: {exc})."
        ) from None

    return ds, WroteSummary("", replace=True)


if __name__ == "__main__":
    fetch()
