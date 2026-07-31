# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cftime",
#   "xarray",
#   "xarray-regrid",
#   "numpy",
# ]
# ///
"""Coarsen/align onto a target grid (geometry only, linear)."""

import math

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import grid_spacing

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"


def _target_axis(coord_vals, resolution, offset):
    import numpy as np

    vmin, vmax = float(np.min(coord_vals)), float(np.max(coord_vals))
    eps = 1e-9 * max(1.0, abs(vmin), abs(vmax)) / resolution
    k_min = math.ceil((vmin - offset) / resolution - eps)
    k_max = math.floor((vmax - offset) / resolution + eps)
    target = offset + np.arange(k_min, k_max + 1) * resolution
    if coord_vals[0] > coord_vals[-1]:
        target = target[::-1]
    return target


@weather_skill(
    name="coarsen",
    version=_SKILL_VERSION,
    inputs=["space"],
    outputs=["space"],
)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--target-resolution", type=float, required=True)
@weather_skill.argument("--offset", type=float, required=True)
def coarsen(ds, variable, target_resolution, offset, **kwargs):
    """Coarsen/align onto a target grid (geometry only, linear)."""
    import numpy as np
    import xarray as xr
    import xarray_regrid  # noqa: F401

    if target_resolution <= 0:
        raise UsageError("--target-resolution must be > 0")
    lat_dim, lon_dim = detect_spatial_dims(ds)
    if variable:
        ds = ds[[variable]]
    lon_vals = np.asarray(ds[lon_dim].values)
    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
        ds = ds.assign_coords({lon_dim: ((ds[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)
    # Reject finer targets (that's downscale)
    for axis in (lat_dim, lon_dim):
        if target_resolution < grid_spacing(ds[axis].values):
            raise UsageError(
                f"--target-resolution {target_resolution}° finer than input; use downscale"
            )
    target = xr.Dataset(
        coords={
            lat_dim: (
                lat_dim,
                _target_axis(ds[lat_dim].values, target_resolution, offset),
                dict(ds[lat_dim].attrs),
            ),
            lon_dim: (
                lon_dim,
                _target_axis(ds[lon_dim].values, target_resolution, offset),
                dict(ds[lon_dim].attrs),
            ),
        }
    )
    return ds.regrid.linear(target)


if __name__ == "__main__":
    coarsen()
