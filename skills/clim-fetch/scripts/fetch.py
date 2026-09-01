# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime",
#   "fsspec",
#   "aiohttp",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch a cached daily climatology from Sheerwater's public GCS mirror.

The mirror stores one static day-of-year climatology per (dataset, variable)
with dims ``init_time``, ``prediction_timedelta``, ``lat``, ``lon`` — 1904
init dates (a leap year, so it always covers day-of-year 1..366 once a lead
is selected). This skill selects one ``--prediction-timedelta`` lead, realizes
``time = init_time + prediction_timedelta``, then expands that static
day-of-year climatology onto every calendar day in a requested
``--start-time``/``--end-time`` window, repeating rows across years as
needed, so timestamps line up with the rest of a pipeline's data.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# Public GCS mirror. Object keys: climatologies/<dataset>_<variable>.zarr
_BUCKET = "sheerwater-public-datalake"
_GCS_MEDIA = f"https://storage.googleapis.com/{_BUCKET}"

# Valid --dataset ids — exactly the bucket's product prefix, no aliasing.
_DATASETS = ("imerg_final", "era5", "chirps")

_DEFAULT_VARIABLE = "precip"
_DEFAULT_LEAD_DAYS = 0


def _open_remote(dataset: str, variable: str) -> xr.Dataset:
    key = f"climatologies/{dataset}_{variable}.zarr"
    url = f"{_GCS_MEDIA}/{key}"
    try:
        return xr.open_zarr(url, consolidated=True)
    except Exception as exc:  # noqa: BLE001 — surface remote open failures cleanly
        raise DataError(
            f"failed to open remote climatology gs://{_BUCKET}/{key} ({exc})."
        ) from None


def _select_lead(clim: xr.Dataset, lead_days: int) -> xr.Dataset:
    """Select one --prediction-timedelta lead and realize valid time = init_time + lead.
    prediction_timedelta is an integer in days.
    """
    available = [int(v) for v in clim["prediction_timedelta"].values]
    if lead_days not in available:
        raise UsageError(
            f"--prediction-timedelta {lead_days} not in this climatology; "
            f"available (days): {sorted(available)}"
        )
    clim = clim.sel(prediction_timedelta=lead_days, drop=True)
    clim = clim.assign_coords(
        init_time=clim["init_time"] + np.timedelta64(lead_days, "D")
    ).rename({"init_time": "time"})
    return clim


def _expand_climatology(clim: xr.Dataset, start, end) -> xr.Dataset:
    """Broadcast day-of-year climatology onto every day in [start, end]."""
    doy = clim["time"].dt.dayofyear.values
    if sorted(doy.tolist()) != list(range(1, 367)):
        raise DataError(
            "expected a full 1904 leap-year climatology (day_of_year 1..366 "
            f"exactly once); got {sorted(set(doy.tolist()))}"
        )

    clim = clim.assign_coords(day_of_year=("time", doy)).swap_dims({"time": "day_of_year"})
    clim = clim.drop_vars("time")

    target_dates = pd.date_range(start, end, freq="D")
    target_doy = xr.DataArray(target_dates.dayofyear, dims="time", coords={"time": target_dates})
    expanded = clim.sel(day_of_year=target_doy)
    return expanded.drop_vars("day_of_year")


@weather_skill(name="clim-fetch", version=_SKILL_VERSION)
@weather_skill.argument(
    "--dataset",
    required=True,
    choices=list(_DATASETS),
    help="Climatology source id.",
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    default=_DEFAULT_VARIABLE,
    help=f"Climate variable (default: {_DEFAULT_VARIABLE}).",
)
@weather_skill.argument(
    "--prediction-timedelta",
    type=int,
    default=_DEFAULT_LEAD_DAYS,
    help=f"Forecast lead in whole days to select (default: {_DEFAULT_LEAD_DAYS}).",
)
@weather_skill.argument("--bbox")
def fetch(dataset, start_time, end_time, variable, prediction_timedelta, bbox, **kwargs):
    """Fetch a cached daily climatology and expand it to the requested date range."""
    print(
        f"clim-fetch: fetching {dataset!r} variable={variable!r} "
        f"prediction_timedelta={prediction_timedelta}d",
        file=sys.stderr,
    )
    clim = _open_remote(dataset, variable)
    clim = _select_lead(clim, prediction_timedelta)

    semantic_name = clim.attrs.get("variable", variable)
    mean_name, std_name = f"{semantic_name}_avg", f"{semantic_name}_std"
    clim = clim.rename({"avg": mean_name, "std": std_name})
    clim = to_standard_units(clim, variables=[mean_name, std_name])

    if bbox is not None:
        # only get necessary chunks
        clim = bbox_subset(clim, bbox)

    expanded = _expand_climatology(clim, start_time, end_time).load()
    expanded = expanded.drop_attrs(deep=False)

    expanded.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"sheerwater-mirror:{dataset}",
        climatology_dataset=dataset,
        climatology_variable=semantic_name,
        climatology_prediction_timedelta_days=prediction_timedelta,
    )
    stamp_cf_attrs(expanded)
    return stamp_data_interval(expanded, period="1 day")


if __name__ == "__main__":
    fetch()
