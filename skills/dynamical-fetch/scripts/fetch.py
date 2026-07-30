# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
#   "dynamical-catalog==0.5.0",
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Fetch a dynamical.org open-catalog dataset and write a WeatherSkills standard dataset."""

import sys
from datetime import date

from weather_skills_core import DataError, EntryOverride, Types, UsageError, weather_skill
from weather_skills_core.dates import np_to_date, parse_date_value
from weather_skills_core.dataset import stamp_cf_attrs

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.13"

_DROP_COORDS = (
    "valid_time",
    "expected_forecast_length",
    "ingested_forecast_length",
    "spatial_ref",
)

_STATE: dict = {}


def _open_dataset(dataset) -> dict:
    """Validate the dataset id, open it, and detect its shape, at most once per run."""
    if "ds" not in _STATE:
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
        _STATE["ds"] = ds
        _STATE["shape"] = shape
    return _STATE


def _latest_from_dataset(dataset) -> date:
    """Newest available date from the opened dataset's own coords."""
    state = _open_dataset(dataset)
    ds = state["ds"]
    is_forecast = state["shape"] in ("ensemble", "forecast")
    coord = "init_time" if is_forecast else "time"
    vals = ds[coord].values
    if is_forecast:
        midnight = vals[vals == vals.astype("datetime64[D]")]
        if midnight.size:
            vals = midnight
    return np_to_date(vals.max())


def _resolve_date(value, dataset: str):
    if value is None:
        return None
    parsed = parse_date_value(value, flag="--date") if isinstance(value, str) else value
    return _latest_from_dataset(dataset) if parsed == "latest" else parsed


def _resolve_window_end(value, dataset: str):
    if value is None:
        return None
    return _latest_from_dataset(dataset) if value == "latest" else value


def _bbox_subset(ds, bbox) -> object:
    """Subset a regular 1-D lat/lon grid to an N/W/S/E bbox."""
    north, west, south, east = bbox
    bbox_raw = f"{north}/{west}/{south}/{east}"
    lat = ds["latitude"].values
    lon = ds["longitude"].values
    lat_slice = slice(north, south) if lat[0] > lat[-1] else slice(south, north)
    lon_slice = slice(west, east) if lon[0] < lon[-1] else slice(east, west)
    ds = ds.sel(latitude=lat_slice, longitude=lon_slice)
    if ds.sizes.get("latitude", 0) == 0 or ds.sizes.get("longitude", 0) == 0:
        raise DataError(
            f"--bbox {bbox_raw} selects no grid cells; check the extent and N/W/S/E order."
        )
    return ds


@weather_skill(
    name="dynamical-fetch",
    version=_SKILL_VERSION,
    outputs=[(Types.GRIDDED, Types.FORECAST)],
    optional_args=("start_time", "end_time", "bbox", "variable"),
    check_cache=True,
)
@weather_skill.argument(
    "--dataset",
    required=True,
    help="Catalog dataset id (validated against dynamical_catalog.list()).",
)
@weather_skill.argument(
    "--date",
    help=(
        "Forecast init date (forecast datasets). YYYY-MM-DD or 'latest'. "
        "Selects the 00 UTC initialization of the resolved date."
    ),
)
def fetch(bbox, dataset, date, start_time, end_time, variable):
    """Fetch a dynamical.org open-catalog dataset and write a WeatherSkills standard dataset."""
    import numpy as np

    state = _open_dataset(dataset)
    ds = state["ds"]
    shape = state["shape"]
    is_forecast = shape in ("ensemble", "forecast")

    override = {}
    if is_forecast:
        if not date:
            raise UsageError(f"{dataset} is a forecast dataset; --date is required.")
        if start_time is not None or end_time is not None:
            raise UsageError(f"{dataset} is a forecast dataset; use --date, not --start/--end.")
        resolved = _resolve_date(date, dataset)
        date_iso = resolved.isoformat()
        override["date"] = date_iso
    else:
        if start_time is None or end_time is None:
            raise UsageError(f"{dataset} is an analysis dataset; --start and --end are required.")
        if date:
            raise UsageError(f"{dataset} is an analysis dataset; use --start/--end, not --date.")
        start_time = _resolve_window_end(start_time, dataset)
        end_time = _resolve_window_end(end_time, dataset)
        start_iso, end_iso = start_time.isoformat(), end_time.isoformat()
        override["start_time"] = start_iso
        override["end_time"] = end_iso

    if bbox:
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

    return ds, EntryOverride(args=override)


if __name__ == "__main__":
    fetch()
