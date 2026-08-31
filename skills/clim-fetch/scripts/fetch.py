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
— 1904 dates on the ``time`` dim (a leap year, so it always covers
day-of-year 1..366). This skill expands that static climatology onto every
calendar day in a requested ``--start-time``/``--end-time`` window, repeating
rows across years as needed, so timestamps line up with the rest of a
pipeline's data.

Grid and region are hardcoded to the only cache that exists today
(``_GRID``/``_REGION`` below); add real ``--grid``/``--region`` support if and
when more caches show up.
"""

from __future__ import annotations

import sys

import pandas as pd
import xarray as xr
from weather_skills_core import DataError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.standard_utils import bbox_subset
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# Public GCS mirror. Object keys: climatologies/<product>_<grid>_<region>_<variable>.zarr
_BUCKET = "sheerwater-public-datalake"
_GCS_MEDIA = f"https://storage.googleapis.com/{_BUCKET}"

_GRID = "global1_5"
_REGION = "global"

# Valid --dataset ids — exactly the bucket's product prefix, no aliasing.
_DATASETS = ("imerg_final", "era5")

_DEFAULT_VARIABLE = "precip"


def _open_remote(dataset: str, variable: str) -> xr.Dataset:
    key = f"climatologies/{dataset}_{_GRID}_{_REGION}_{variable}.zarr"
    url = f"{_GCS_MEDIA}/{key}"
    try:
        return xr.open_zarr(url, consolidated=True)
    except Exception as exc:  # noqa: BLE001 — surface remote open failures cleanly
        raise DataError(
            f"failed to open remote climatology gs://{_BUCKET}/{key} ({exc})."
        ) from None


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
@weather_skill.argument("--bbox")
def fetch(dataset, start_time, end_time, variable, bbox, **kwargs):
    """Fetch a cached daily climatology and expand it to the requested date range."""
    print(
        f"clim-fetch: fetching {dataset!r} variable={variable!r}",
        file=sys.stderr,
    )
    clim = _open_remote(dataset, variable)

    semantic_name = clim.attrs.get("variable", variable)
    mean_name, variance_name = f"{semantic_name}_avg", f"{semantic_name}_variance"
    clim = clim.rename({"avg": mean_name, "variance": variance_name})
    clim = to_standard_units(clim, variables=[mean_name, variance_name])

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
        climatology_grid=_GRID,
        climatology_region=_REGION,
    )
    stamp_cf_attrs(expanded)
    return stamp_data_interval(expanded, period="1 day")


if __name__ == "__main__":
    fetch()
