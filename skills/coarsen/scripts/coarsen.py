# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
#   "xarray",
#   "xarray-regrid",
#   "numpy",
# ]
# ///
"""Coarsen or align a weather-skills envelope Zarr onto a target grid (geometry only).

Generates a target grid at points ``offset + k * resolution`` for integer k,
clipped to the input's lon/lat range, and interpolates onto it linearly via
xarray-regrid. This changes grid geometry only and adds no information; it is
used to coarsen a grid or to align two grids for comparison. ``(0.25, 0.0)``
aligns with sheerwater's ``global0_25``; ``(0.1, 0.05)`` with ``global0_1``;
``(0.05, 0.025)`` with ``global0_05``.
"""

import math
import sys

from weather_skills_core import UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"


def _grid_spacing(coord_vals) -> float:
    import numpy as np

    coord = np.asarray(coord_vals)
    if coord.size < 2:
        raise ValueError(f"Cannot infer spacing for coord with size {coord.size}")
    return float(abs(np.median(np.diff(coord))))


def _target_axis(coord_vals, resolution: float, offset: float):
    import numpy as np

    vmin = float(np.min(coord_vals))
    vmax = float(np.max(coord_vals))
    # Tolerance on (vmin, vmax) - offset / resolution to keep boundary points
    # that are on-grid up to floating-point noise.
    eps = 1e-9 * max(1.0, abs(vmin), abs(vmax)) / resolution
    k_min = math.ceil((vmin - offset) / resolution - eps)
    k_max = math.floor((vmax - offset) / resolution + eps)
    if k_max < k_min:
        raise ValueError(
            f"No grid points at offset={offset}, resolution={resolution} "
            f"fall within range [{vmin}, {vmax}]."
        )
    target = offset + np.arange(k_min, k_max + 1) * resolution
    if coord_vals[0] > coord_vals[-1]:
        target = target[::-1]
    return target


def _validate_args(args):
    if args.target_resolution <= 0:
        raise UsageError("--target-resolution must be > 0.")


@weather_skill(
    "coarsen",
    _SKILL_VERSION,
    input_type="any",
    output_type="same",
    variable={"mode": "single", "help": "Restrict to a single data variable."},
    dims=True,
    extra_args={
        "target_resolution": {
            "type": float,
            "required": True,
            "help": "Target grid spacing in degrees.",
        },
        "offset": {
            "type": float,
            "required": True,
            "help": "Grid offset in degrees; target points fall at offset + k*resolution.",
        },
    },
    validate_args=_validate_args,
    hash_input=False,
)
def coarsen(ds, variable, dims, target_resolution, offset):
    """Coarsen or align a weather-skills envelope Zarr onto a target grid (geometry only)."""
    import numpy as np
    import xarray as xr
    import xarray_regrid  # noqa: F401 — registers the .regrid accessor
    from weather_skills_core.envelope import detect_spatial_dims

    lat_dim, lon_dim = detect_spatial_dims(ds, dims)

    if variable:
        if variable not in ds.data_vars:
            raise UsageError(f"variable '{variable}' not in {list(ds.data_vars)}")
        ds = ds[[variable]]

    # Wrap lon to [-180, 180] before building the target axis so a 0..360 input
    # grid doesn't produce a target axis spanning ~the whole globe. Mirrors plot.py.
    lon_vals = np.asarray(ds[lon_dim].values)
    if lon_vals.size and float(np.nanmax(lon_vals)) > 180.0:
        ds = ds.assign_coords({lon_dim: ((ds[lon_dim] + 180) % 360 - 180)}).sortby(lon_dim)

    # Coarsen goes coarser-or-equal. Reject a strictly-finer target on either
    # axis (target spacing smaller than the input spacing) — that is the
    # downscale skill's job. Coarser-or-equal passes (equal = no-op/realign).
    in_lat_res = _grid_spacing(ds[lat_dim].values)
    in_lon_res = _grid_spacing(ds[lon_dim].values)
    if target_resolution < in_lat_res or target_resolution < in_lon_res:
        raise UsageError(
            f"--target-resolution {target_resolution}° is finer "
            f"than the input on at least one axis "
            f"(~{in_lat_res:.4f}°x{in_lon_res:.4f}°). "
            f"Coarsening goes coarser-or-equal; to make a grid finer and add "
            f"information use the downscale skill."
        )

    new_lat = _target_axis(ds[lat_dim].values, target_resolution, offset)
    new_lon = _target_axis(ds[lon_dim].values, target_resolution, offset)
    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )

    print(
        f"Coarsening/aligning {lat_dim},{lon_dim} (linear) to "
        f"resolution={target_resolution} offset={offset}: "
        f"{ds.sizes[lat_dim]}x{ds.sizes[lon_dim]} -> {len(new_lat)}x{len(new_lon)}",
        file=sys.stderr,
    )
    return ds.regrid.linear(target)


if __name__ == "__main__":
    coarsen()
