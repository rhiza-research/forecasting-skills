# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cftime",
#   "dynamical-catalog==0.5.0",
#   "xarray",
#   "zarr",
#   "numpy",
#   "cf-units>=3.3",
# ]
# ///
"""Fetch a dynamical.org open-catalog dataset and write a weather-skills envelope Zarr."""

import sys

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.dates import np_to_date
from weather_skills_core.dataset import stamp_cf_attrs
from weather_skills_core.units import to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.13"

# Coords dynamical attaches that are not part of the weather-skills envelope: forecast
# bookkeeping (valid_time, *_forecast_length) and the CRS scalar (spatial_ref).
# Dropped on the way out so the output carries only envelope coords.
_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)

def _open_dataset(state, dataset) -> dict:
    """Validate the dataset id, open it, and detect its shape, at most once per run."""
    if "ds" not in state:
        import dynamical_catalog

        catalog = dynamical_catalog.list()
        if dataset not in catalog:
            raise UsageError(
                f"unknown dataset {dataset!r}. Available datasets:\n  " + "\n  ".join(catalog)
            )
        ds = dynamical_catalog.open(dataset)

        if "latitude" not in ds.dims or "longitude" not in ds.dims:
            raise UsageError(
                f"{dataset} is on a projected grid (dims {tuple(ds.dims)}); this "
                "fetcher only handles regular 1-D latitude/longitude grids. Reprojecting a "
                "projected grid to lat/lon is a grid transform for a dedicated reprojection "
                "skill, not this fetcher."
            )

        if "ensemble_member" in ds.dims:
            shape = "ensemble"
        elif "lead_time" in ds.dims:
            shape = "forecast"
        elif "time" in ds.dims:
            shape = "analysis"
        else:
            raise UsageError(
                f"{dataset} has an unrecognized shape (dims {tuple(ds.dims)}); "
                "expected an ensemble/deterministic forecast (lead_time) or an analysis (time)."
            )
        state["ds"] = ds
        state["shape"] = shape
    return state

def _bbox_subset(ds, bbox) -> object:
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox."""
    north, west, south, east = bbox
    lat = ds["latitude"].values
    lon = ds["longitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    lon_slice = slice(west, east) if lon[0] < lon[-1] else slice(east, west)
    ds = ds.sel(latitude=lat_slice, longitude=lon_slice)
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        raise DataError(
            f"--bbox {north}/{west}/{south}/{east} selects no grid cells; "
            "check the extent and N/W/S/E order."
        )
    return ds

@weather_skill(
    name="dynamical-fetch",
    version=_SKILL_VERSION,
    outputs=[["observations", "forecast", "ensemble_forecast"]]
)
@weather_skill.argument("--date")
@weather_skill.argument("--start-time")
@weather_skill.argument("--end-time")
@weather_skill.argument("--bbox")
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
            "--dataset",
            required=True,
            help="Catalog dataset id (validated against dynamical_catalog.list()).",
        )
def fetch(bbox, dataset, date, start_time, end_time, variable, **kwargs):
    """Fetch a dynamical.org open-catalog dataset and write a weather-skills envelope Zarr."""
    import numpy as np

    state = {}
    state = _open_dataset(state, dataset)
    ds = state["ds"]
    shape = state["shape"]
    is_forecast = shape in ("ensemble", "forecast")

    if is_forecast:
        if date is None:
            raise UsageError(f"{dataset} is a forecast dataset; --date is required.")
        if start_time is not None or end_time is not None:
            raise UsageError(f"{dataset} is a forecast dataset; use --date, not --start-time/--end-time.")
        date_iso = date.isoformat()
    else:
        if start_time is None or end_time is None:
            raise UsageError(f"{dataset} is an analysis dataset; --start-time and --end-time are required.")
        if date is not None:
            raise UsageError(f"{dataset} is an analysis dataset; use --start-time/--end-time, not --date.")
        start_iso = start_time.isoformat()
        end_iso = end_time.isoformat()

    if bbox is not None:
        ds = _bbox_subset(ds, bbox)

    if is_forecast:
        inits = ds["init_time"].values
        init_target = np.datetime64(f"{date_iso}T00:00:00").astype(inits.dtype)

        def _no_init() -> DataError:
            return DataError(
                f"{dataset} has no {date_iso} 00 UTC init; available init range is "
                f"{np_to_date(inits.min()).isoformat()}..{np_to_date(inits.max()).isoformat()}."
            )

        if init_target not in inits:
            raise _no_init()
        try:
            ds = ds.sel(init_time=init_target)
        except KeyError:
            raise _no_init() from None
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])
        rename = {"lead_time": "step"}
        if shape == "ensemble":
            rename["ensemble_member"] = "number"
        ds = ds.rename(rename)
        ds = ds.assign_coords(time=ds["init_time"]).drop_vars("init_time")
    else:
        ds = ds.sel(time=slice(np.datetime64(start_iso), np.datetime64(end_iso)))
        if ds.sizes.get("time", 0) == 0:
            raise DataError(f"{dataset} has no data in {start_iso}..{end_iso}.")
        ds = ds.drop_vars([c for c in _DROP_COORDS if c in ds.coords])

    if variable:
        missing = [v for v in variable if v not in ds.data_vars]
        if missing:
            raise UsageError(
                f"variable(s) not in {dataset}: {', '.join(missing)}.\n"
                f"Available: {', '.join(sorted(ds.data_vars))}"
            )
        ds = ds[variable]

    print(f"Fetching dynamical:{dataset} (shape={shape})", file=sys.stderr)

    ds.attrs.update(
        weather_skills_source=f"dynamical:{dataset}",
        Conventions="CF-1.13",
    )
    stamp_cf_attrs(ds)
    ds = to_standard_units(ds)

    return ds

if __name__ == "__main__":
    fetch()
