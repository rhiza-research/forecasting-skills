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
"""Fetch a cached climatology from Sheerwater's public GCS mirror.

Source dims: ``init_time``, ``prediction_timedelta``, ``lat``, ``lon`` — 1904
init dates (a leap year, so day-of-year always covers 1..366). Pipeline:
select one ``--prediction-timedelta`` lead (``time = init_time + lead``),
optionally roll up to a coarser ``--window`` (days), then expand onto every
day in ``--start-time``/``--end-time``, repeating rows across years as needed.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset, roll_and_agg
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# Public GCS mirror. Object keys, one convention for every window (daily is
# just window=1): climatologies/<dataset>_<variable>_<window>d.zarr
_BUCKET = "sheerwater-public-datalake"
_GCS_MEDIA = f"https://storage.googleapis.com/{_BUCKET}"

# Valid --dataset ids — exactly the bucket's product prefix, no aliasing.
_DATASETS = ("imerg_final", "era5", "chirps")

_DEFAULT_VARIABLE = "precip"
_DEFAULT_LEAD_DAYS = 0
_DEFAULT_WINDOW_DAYS = 1


def _object_key(dataset: str, variable: str, window: int) -> str:
    return f"climatologies/{dataset}_{variable}_{window}d.zarr"


def _open_zarr_or_none(key: str) -> xr.Dataset | None:
    url = f"{_GCS_MEDIA}/{key}"
    try:
        return xr.open_zarr(url, consolidated=True)
    except Exception:  # noqa: BLE001 — probing for existence, caller decides what's fatal
        return None


def _open_remote(dataset: str, variable: str, window: int) -> tuple[xr.Dataset, bool]:
    """Open the climatology; return (dataset, is_pre_aggregated).

    Tries the exact --window cache; falls back to the daily (1d) cache —
    which the caller must then roll up itself.
    """
    if window > 1:
        pre_aggregated = _open_zarr_or_none(_object_key(dataset, variable, window))
        if pre_aggregated is not None:
            return pre_aggregated, True
        print(
            f"clim-fetch: no pre-aggregated {window}d cache for "
            f"{dataset!r}/{variable!r}; falling back to daily + local rolling",
            file=sys.stderr,
        )
    key = _object_key(dataset, variable, 1)
    daily = _open_zarr_or_none(key)
    if daily is None:
        raise DataError(f"failed to open remote climatology gs://{_BUCKET}/{key}.")
    return daily, False


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


def _pad_circular(clim: xr.Dataset, window: int) -> xr.Dataset:
    """Wrap ``window`` days from each end of the 366-day cycle onto the
    opposite side, shifted by a full cycle so ``time`` stays monotonic.
    Ensures windowed aggregation always sees a full window of values.
    """
    n = clim.sizes["time"]
    cycle = np.timedelta64(n, "D")
    before = clim.isel(time=slice(-window, None))
    before = before.assign_coords(time=before["time"] - cycle)
    after = clim.isel(time=slice(0, window))
    after = after.assign_coords(time=after["time"] + cycle)
    return xr.concat([before, clim, after], dim="time")


def _roll_climatology(clim: xr.Dataset, mean_name: str, std_name: str, window: int) -> xr.Dataset:
    """Roll a daily climatology up to a coarser --window, correctly handling different
    aggregation approach for mean and std.
    """
    padded = _pad_circular(clim, window)
    original_times = clim["time"].values

    rolled_mean = roll_and_agg(padded[[mean_name]], window, "time", "mean", align="center")
    rolled_mean = rolled_mean.sel(time=original_times)

    squared = (padded[std_name] ** 2).to_dataset(name=std_name)
    squared_mean = roll_and_agg(squared, window, "time", "mean", align="center")
    squared_mean = squared_mean.sel(time=original_times)
    std_out = np.sqrt(squared_mean[std_name] / window)
    std_out.attrs = dict(clim[std_name].attrs)  # sqrt(mean(x^2))/sqrt(N) keeps x's units

    out = rolled_mean
    out[std_name] = std_out
    return out


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
@weather_skill.argument(
    "--window",
    type=int,
    default=_DEFAULT_WINDOW_DAYS,
    help=(
        f"Climatology window in days (default: {_DEFAULT_WINDOW_DAYS}, i.e. "
        "daily). Tries a pre-aggregated cache first; falls back to rolling up "
        "the daily climatology locally (centered window) if that isn't mirrored."
    ),
)
@weather_skill.argument("--bbox")
def fetch(dataset, start_time, end_time, variable, prediction_timedelta, window, bbox, **kwargs):
    """Fetch a cached daily climatology and expand it to the requested date range."""
    if window < 1:
        raise UsageError(f"--window must be >= 1; got {window}")
    print(
        f"clim-fetch: fetching {dataset!r} variable={variable!r} "
        f"prediction_timedelta={prediction_timedelta}d window={window}d",
        file=sys.stderr,
    )
    clim, is_pre_aggregated = _open_remote(dataset, variable, window)
    clim = _select_lead(clim, prediction_timedelta)

    semantic_name = clim.attrs.get("variable", variable)
    mean_name, std_name = f"{semantic_name}_avg", f"{semantic_name}_std"
    clim = clim.rename({"avg": mean_name, "std": std_name})
    clim = to_standard_units(clim, variables=[mean_name, std_name])

    if bbox is not None:
        # Before the roll: only pull/compute the bbox's chunks, not the
        # whole global grid — rolling+padding the full globe is far more
        # expensive than the subset for a local (non-pre-aggregated) window.
        clim = bbox_subset(clim, bbox)

    if window > 1 and not is_pre_aggregated:
        clim = _roll_climatology(clim, mean_name, std_name, window)

    expanded = _expand_climatology(clim, start_time, end_time).load()
    expanded = expanded.drop_attrs(deep=False)

    expanded.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source=f"sheerwater-mirror:{dataset}",
        climatology_dataset=dataset,
        climatology_variable=semantic_name,
        climatology_prediction_timedelta_days=prediction_timedelta,
        climatology_window_days=window,
    )
    stamp_cf_attrs(expanded)
    return stamp_data_interval(expanded, period="1 day")


if __name__ == "__main__":
    fetch()
