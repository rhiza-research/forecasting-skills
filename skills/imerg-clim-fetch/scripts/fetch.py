# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "sheerwater",
#   "xarray",
#   "zarr",
#   "numpy",
#   "cftime",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch IMERG daily climatology (1998–2023 baseline) via Sheerwater and write a weather-skills standard dataset Zarr."""

import sys

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.units import stamp_data_interval, to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_GRIDS = ("global0_25", "global1_5")
_CLIM_FIRST_YEAR = 1998
_CLIM_LAST_YEAR = 2023
_CLIM_DATA_SOURCE = "imerg_final"


def _forecast_to_daily_climatology(ds):
    """Collapse Sheerwater forecast layout to ``(time, lat, lon)``."""
    import numpy as np

    if "prediction_timedelta" in ds.dims:
        zero = np.timedelta64(0, "ns")
        if zero in ds["prediction_timedelta"].values:
            ds = ds.sel(prediction_timedelta=zero, drop=True)
        else:
            ds = ds.isel(prediction_timedelta=0, drop=True)
    if "init_time" in ds.dims:
        ds = ds.rename({"init_time": "time"})
    if "prediction_timedelta" in ds.dims:
        ds = ds.drop_vars("prediction_timedelta")
    return ds


@weather_skill(
    name="imerg-clim-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--variable", "-v", default="precip")
@weather_skill.argument(
    "--grid",
    default="global0_25",
    choices=list(_GRIDS),
    help="Sheerwater target grid (global0_25 = 0.25°, global1_5 = 1.5°).",
)
@weather_skill.argument(
    "--region",
    default="global",
    help="Sheerwater spatial region passed through to the climatology accessor (default: global).",
)
def fetch(start_time, end_time, variable, grid, region, **kwargs):
    """Fetch IMERG daily climatology via Sheerwater and write a weather-skills standard dataset Zarr."""
    if variable != "precip":
        raise UsageError("only --variable precip is supported for IMERG climatology")

    start = start_time.isoformat()
    end = end_time.isoformat()

    from sheerwater.climatology import climatology_imerg_1998_2024

    print(
        f"Fetching IMERG climatology {_CLIM_FIRST_YEAR}-{_CLIM_LAST_YEAR} "
        f"for {start} -> {end} on grid={grid} region={region}",
        file=sys.stderr,
    )
    ds = climatology_imerg_1998_2024(start, end, variable, grid=grid, region=region)
    ds = _forecast_to_daily_climatology(ds)
    ds = ds.load()
    ds = ds.drop_attrs()
    ds.attrs.update(
        Conventions="CF-1.13",
        weather_skills_source="sheerwater:climatology_imerg_1998_2024",
        climatology_first_year=_CLIM_FIRST_YEAR,
        climatology_last_year=_CLIM_LAST_YEAR,
        climatology_data_source=_CLIM_DATA_SOURCE,
        climatology_grid=grid,
        climatology_region=region,
    )
    if variable in ds:
        ds[variable].attrs.setdefault("long_name", "IMERG daily precipitation climatology")
    stamp_cf_attrs(ds)
    ds = to_standard_units(ds, variables=[variable])
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()
