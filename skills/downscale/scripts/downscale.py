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
"""Downscale onto a finer grid (linear or empirical q-q)."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims, detect_time_dim
from weather_skills_core.standard_utils import (
    ensure_normalized_longitude,
    grid_spacing,
    spacing_is_finer,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _spacing(ds, dim):
    return grid_spacing(ds[dim].values)


def _target_coord(coord, new_spacing):
    import numpy as np

    vmin, vmax = float(np.min(coord)), float(np.max(coord))
    target = vmin + new_spacing * np.arange(int(np.floor((vmax - vmin) / new_spacing)) + 1)
    return target[::-1] if coord[0] > coord[-1] else target


def _qmap_1d(model, ref):
    import numpy as np

    out = np.full(model.shape, np.nan, dtype=float)
    m_ok, r_ok = ~np.isnan(model), ~np.isnan(ref)
    if not m_ok.any() or not r_ok.any():
        return out
    sorted_m, sorted_r = np.sort(model[m_ok]), np.sort(ref[r_ok])
    ranks = np.searchsorted(sorted_m, model[m_ok], side="right")
    quants = np.clip((ranks - 0.5) / sorted_m.size, 0.0, 1.0)
    out[m_ok] = np.interp(quants, (np.arange(sorted_r.size) + 0.5) / sorted_r.size, sorted_r)
    return out


@weather_skill(
    name="downscale",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--algorithm",
    required=True,
    choices=["linear-interpolation", "q-q"],
)
@weather_skill.argument("-f", "--factor", type=int, default=None)
@weather_skill.argument("--target-resolution", type=float, default=None)
@weather_skill.argument("--reference-grid", default=None)
@weather_skill.argument("--qq-reference", default=None)
@weather_skill.argument("--time-dim", default="time")
def downscale(
    ds,
    variable,
    algorithm,
    factor,
    target_resolution,
    reference_grid,
    qq_reference,
    time_dim,
    **kwargs,
):
    """Downscale onto a finer grid (linear or empirical q-q)."""
    import numpy as np
    import xarray as xr
    import xarray_regrid  # noqa: F401

    n = sum(x is not None for x in (factor, target_resolution, reference_grid))
    if n != 1:
        raise UsageError("exactly one of --factor, --target-resolution, --reference-grid")
    if algorithm == "q-q" and not qq_reference:
        raise UsageError("--algorithm q-q requires --qq-reference")
    if qq_reference and algorithm != "q-q":
        raise UsageError("--qq-reference only valid with --algorithm q-q")

    lat_dim, lon_dim = detect_spatial_dims(ds)
    if variable:
        ds = ds[[variable]]
    ds = ensure_normalized_longitude(ds, lon_dim)
    in_lat, in_lon = _spacing(ds, lat_dim), _spacing(ds, lon_dim)

    if reference_grid is not None:
        ref = xr.open_zarr(reference_grid, consolidated=False)
        new_lat, new_lon = np.asarray(ref[lat_dim].values), np.asarray(ref[lon_dim].values)
        if spacing_is_finer(in_lat, _spacing(ref, lat_dim)) or spacing_is_finer(
            in_lon, _spacing(ref, lon_dim)
        ):
            raise UsageError("--reference-grid is coarser than input; use coarsen")
    else:
        if factor is not None:
            if factor < 1:
                raise UsageError("--factor must be >= 1")
            lat_sp, lon_sp = in_lat / factor, in_lon / factor
        else:
            if target_resolution <= 0 or spacing_is_finer(
                in_lat, target_resolution
            ) or spacing_is_finer(in_lon, target_resolution):
                raise UsageError("--target-resolution must be finer-or-equal to input")
            lat_sp = lon_sp = target_resolution
        new_lat = _target_coord(ds[lat_dim].values, lat_sp)
        new_lon = _target_coord(ds[lon_dim].values, lon_sp)

    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )
    out = ds.regrid.linear(target)

    if algorithm == "q-q":
        time_dim = detect_time_dim(out, time_dim)
        ref = xr.open_zarr(qq_reference, consolidated=False)
        for d in (lat_dim, lon_dim):
            if not np.allclose(out[d].values, ref[d].values, atol=1e-6, rtol=0):
                raise UsageError(f"--qq-reference '{d}' must match post-downscale grid")
        ref = ref.assign_coords({lat_dim: out[lat_dim], lon_dim: out[lon_dim]})
        for v in out.data_vars:
            if v not in ref.data_vars:
                continue
            mapped = xr.apply_ufunc(
                _qmap_1d,
                out[v],
                ref[v].rename({time_dim: "_qq_ref_time"}),
                input_core_dims=[[time_dim], ["_qq_ref_time"]],
                output_core_dims=[[time_dim]],
                vectorize=True,
                output_dtypes=[float],
            )
            mapped.attrs = dict(out[v].attrs)
            out[v] = mapped
    return out


if __name__ == "__main__":
    downscale()
