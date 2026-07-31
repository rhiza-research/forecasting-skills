# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cftime",
#   "numpy",
#   "xarray",
# ]
# ///
"""Spatially subset a gridded weather-skills standard dataset Zarr."""

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"


@weather_skill(
    name="clip-region",
    version=_SKILL_VERSION,
    inputs=["space"],
    outputs=["space"],
)
@weather_skill.argument("--bbox", required=True)
def clip_region(ds, bbox, **kwargs):
    """Spatially subset a gridded weather-skills standard dataset Zarr."""
    import numpy as np
    import xarray as xr

    # Decorator already parsed --bbox into (N, W, S, E) floats.
    north, west, south, east = bbox
    lat_dim, lon_dim = detect_spatial_dims(ds)

    lon_vals = np.asarray(ds[lon_dim].values)
    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
        ds = ds.assign_coords({lon_dim: ((ds[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)
        lon_vals = np.asarray(ds[lon_dim].values)
    if lon_vals.size == 0:
        raise UsageError("lon axis has length 0; cannot subset.")
    if lon_vals.size == 1:
        lon_ascending = True
    else:
        lon_diffs = np.diff(lon_vals)
        if (lon_diffs > 0).all():
            lon_ascending = True
        elif (lon_diffs < 0).all():
            lon_ascending = False
        else:
            raise UsageError("lon axis is non-monotonic; cannot infer slice orientation.")

    lat_vals = np.asarray(ds[lat_dim].values)
    if lat_vals.size == 0:
        raise UsageError("lat axis has length 0; cannot subset.")
    if lat_vals.size == 1:
        lat_sel = None
    else:
        diffs = np.diff(lat_vals)
        if (diffs > 0).all():
            lat_sel = slice(south, north)
        elif (diffs < 0).all():
            lat_sel = slice(north, south)
        else:
            raise UsageError("lat axis is non-monotonic; cannot infer slice orientation.")
    if lat_sel is not None:
        ds = ds.sel({lat_dim: lat_sel})

    if west <= east:
        lon_sel = slice(west, east) if lon_ascending else slice(east, west)
        ds = ds.sel({lon_dim: lon_sel})
    else:
        if lon_ascending:
            wings = [ds.sel({lon_dim: slice(None, east)}), ds.sel({lon_dim: slice(west, None)})]
        else:
            wings = [ds.sel({lon_dim: slice(None, west)}), ds.sel({lon_dim: slice(east, None)})]
        ds = xr.concat(
            wings,
            dim=lon_dim,
            data_vars="minimal",
            coords="minimal",
            compat="override",
            join="exact",
        )

    if ds.sizes.get(lat_dim, 0) == 0 or ds.sizes.get(lon_dim, 0) == 0:
        bbox_str = f"{north}/{west}/{south}/{east}"
        if west > east:
            raise DataError(
                f"--bbox {bbox_str} crosses the antimeridian but selects no grid cells."
            )
        raise DataError(f"--bbox {bbox_str} selects no grid cells.")
    return ds


if __name__ == "__main__":
    clip_region()
