# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "xarray",
#   "xarray-regrid",
#   "numpy",
# ]
# ///
"""Coarsen/align onto a target grid (geometry only, linear)."""

import math

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import (
    ensure_normalized_longitude,
    grid_spacing,
    spacing_is_finer,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


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


def _clip_reference_axis(input_vals, ref_vals):
    """Keep reference coordinate values that fall within the input span."""
    import numpy as np

    ref = np.asarray(ref_vals, dtype=float)
    if ref.size == 0:
        raise UsageError("--reference-grid has an empty lat/lon axis")
    vmin, vmax = float(np.min(input_vals)), float(np.max(input_vals))
    if ref.size > 1:
        spacing = float(np.median(np.abs(np.diff(np.sort(np.unique(ref))))))
    else:
        spacing = 0.0
    pad = 0.5 * spacing
    kept = ref[(ref >= vmin - pad) & (ref <= vmax + pad)]
    if kept.size == 0:
        raise UsageError(
            "--reference-grid lat/lon does not overlap the input spatial extent; "
            "nothing to regrid onto"
        )
    # Match the input's monotonic direction so regrid gets a consistent axis.
    if input_vals[0] > input_vals[-1] and kept.size > 1 and kept[0] < kept[-1]:
        kept = kept[::-1].copy()
    elif input_vals[0] < input_vals[-1] and kept.size > 1 and kept[0] > kept[-1]:
        kept = kept[::-1].copy()
    return kept


@weather_skill(
    name="coarsen",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--reference-grid",
    default=None,
    help=(
        "Zarr whose lat/lon become the target (exact coordinate values). "
        "Prefer this when matching another dataset for difference/verify."
    ),
)
@weather_skill.argument(
    "--target-resolution",
    type=float,
    default=None,
    help="Target spacing in degrees (with --offset). Prefer --reference-grid to match a Zarr.",
)
@weather_skill.argument(
    "--offset",
    type=float,
    default=None,
    help="Grid offset in degrees; points at offset + k*resolution (with --target-resolution).",
)
def coarsen(ds, variable, reference_grid, target_resolution, offset, **kwargs):
    """Coarsen/align onto a target grid (geometry only, linear)."""
    import numpy as np
    import xarray as xr
    import xarray_regrid  # noqa: F401

    has_ref = reference_grid is not None
    has_pair = target_resolution is not None and offset is not None
    has_partial = (target_resolution is not None) ^ (offset is not None)
    if has_ref and (target_resolution is not None or offset is not None):
        raise UsageError(
            "pass --reference-grid alone, or --target-resolution with --offset, not both"
        )
    if has_partial:
        raise UsageError("--target-resolution and --offset must be passed together")
    if not has_ref and not has_pair:
        raise UsageError(
            "pass --reference-grid PATH (preferred when matching another Zarr), "
            "or both --target-resolution and --offset"
        )
    if target_resolution is not None and target_resolution <= 0:
        raise UsageError("--target-resolution must be > 0")

    lat_dim, lon_dim = detect_spatial_dims(ds)
    if variable:
        ds = ds[[variable]]
    ds = ensure_normalized_longitude(ds, lon_dim)
    in_lat, in_lon = grid_spacing(ds[lat_dim].values), grid_spacing(ds[lon_dim].values)

    if has_ref:
        ref = xr.open_zarr(reference_grid, consolidated=False)
        ref_lat_dim, ref_lon_dim = detect_spatial_dims(ref)
        ref = ensure_normalized_longitude(ref, ref_lon_dim)
        ref_lat_sp = grid_spacing(ref[ref_lat_dim].values)
        ref_lon_sp = grid_spacing(ref[ref_lon_dim].values)
        if spacing_is_finer(ref_lat_sp, in_lat) or spacing_is_finer(ref_lon_sp, in_lon):
            raise UsageError(
                "--reference-grid is finer than input; use downscale --reference-grid"
            )
        new_lat = _clip_reference_axis(ds[lat_dim].values, ref[ref_lat_dim].values)
        new_lon = _clip_reference_axis(ds[lon_dim].values, ref[ref_lon_dim].values)
        lat_attrs = dict(ref[ref_lat_dim].attrs) or dict(ds[lat_dim].attrs)
        lon_attrs = dict(ref[ref_lon_dim].attrs) or dict(ds[lon_dim].attrs)
    else:
        # Reject finer targets (that's downscale)
        for spacing in (in_lat, in_lon):
            if spacing_is_finer(target_resolution, spacing):
                raise UsageError(
                    f"--target-resolution {target_resolution}° finer than input; use downscale"
                )
        new_lat = _target_axis(ds[lat_dim].values, target_resolution, offset)
        new_lon = _target_axis(ds[lon_dim].values, target_resolution, offset)
        lat_attrs = dict(ds[lat_dim].attrs)
        lon_attrs = dict(ds[lon_dim].attrs)

    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, lat_attrs),
            lon_dim: (lon_dim, new_lon, lon_attrs),
        }
    )
    return ds.regrid.linear(target)


if __name__ == "__main__":
    coarsen()
