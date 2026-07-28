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
"""Downscale a weather-skills envelope Zarr onto a finer grid via a chosen algorithm."""

import sys

from weather_skills_core import UsageError, WroteSummary, types, validate_type, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"


def _grid_spacing(ds, dim):
    import numpy as np

    coord = ds[dim].values
    if coord.size < 2:
        raise ValueError(f"Cannot infer spacing for dim '{dim}' with size {coord.size}")
    return float(abs(np.median(np.diff(coord))))


def _target_coord(coord, new_spacing):
    import numpy as np

    vmin = float(np.min(coord))
    vmax = float(np.max(coord))
    n = int(np.floor((vmax - vmin) / new_spacing)) + 1
    target = vmin + new_spacing * np.arange(n)
    if coord[0] > coord[-1]:
        target = target[::-1]
    return target


def _empirical_qmap_1d(model, ref):
    import numpy as np

    out = np.full(model.shape, np.nan, dtype=float)
    m_valid = ~np.isnan(model)
    r_valid = ~np.isnan(ref)
    if not m_valid.any() or not r_valid.any():
        return out
    sorted_ref = np.sort(ref[r_valid])
    sorted_model = np.sort(model[m_valid])
    n_m = sorted_model.size
    n_r = sorted_ref.size
    ranks = np.searchsorted(sorted_model, model[m_valid], side="right")
    quants = np.clip((ranks - 0.5) / n_m, 0.0, 1.0)
    ref_q = (np.arange(n_r) + 0.5) / n_r
    out[m_valid] = np.interp(quants, ref_q, sorted_ref)
    return out


def _qmap_dataarray(model_da, ref_da, time_dim):
    import xarray as xr

    ref_renamed = ref_da.rename({time_dim: "_qq_ref_time"})
    return xr.apply_ufunc(
        _empirical_qmap_1d,
        model_da,
        ref_renamed,
        input_core_dims=[[time_dim], ["_qq_ref_time"]],
        output_core_dims=[[time_dim]],
        vectorize=True,
        output_dtypes=[float],
    )


def _coords_match(a, b, atol=1e-6):
    import numpy as np

    if a.shape != b.shape:
        return False
    return bool(np.allclose(a, b, atol=atol, rtol=0))


def _validate_args(args):
    if args.factor is not None and args.factor < 1:
        raise UsageError("--factor must be >= 1.")
    if args.target_resolution is not None and args.target_resolution <= 0:
        raise UsageError("--target-resolution must be > 0.")
    if args.algorithm == "q-q" and not args.qq_reference:
        raise UsageError(
            "--algorithm q-q requires --qq-reference (the distribution reference to map onto)."
        )
    if args.qq_reference and args.algorithm != "q-q":
        raise UsageError("--qq-reference is only valid with --algorithm q-q.")


@weather_skill(
    "downscale",
    _SKILL_VERSION,
    input_type=types.ALL,
    output_type=types.ALL,
    variable={"mode": types.SINGLE, "help": "Restrict to a single data variable"},
    dims=True,
    time_dim=True,
    extra_args={
        "algorithm": {
            "required": True,
            "choices": ["linear-interpolation", "q-q"],
            "help": (
                "Which downscaling algorithm adds information when going finer. "
                "'linear-interpolation' linearly interpolates onto the finer grid; "
                "'q-q' interpolates and then empirically quantile-maps onto a "
                "distribution reference."
            ),
        },
        "factor": {
            "aliases": ["-f"],
            "type": int,
            "help": "Integer refinement factor (>= 1). New spacing = input spacing / factor.",
        },
        "target_resolution": {
            "type": float,
            "help": "Target grid spacing in degrees. Must be finer-or-equal (<=) to the input.",
        },
        "reference_grid": {
            "help": (
                "Path to a reference Zarr whose lat/lon grid defines the finer "
                "target. The reference grid must be finer-or-equal to the input."
            ),
        },
        "qq_reference": {
            "help": (
                "Reference Zarr whose distribution the q-q method maps the output "
                "onto. Empirical quantile mapping per grid cell along --time-dim. "
                "The reference must already be on the post-downscale lat/lon grid. "
                "Required for --algorithm q-q."
            ),
        },
    },
    mutex_groups={
        "target": {"args": ("factor", "target_resolution", "reference_grid"), "required": True},
    },
    validate_args=_validate_args,
    reference_args=("reference_grid", "qq_reference"),
    hash_input=False,
)
def downscale(
    ds, variable, dims, time_dim, algorithm, factor, target_resolution, reference_grid, qq_reference
):
    """Downscale a weather-skills envelope Zarr onto a finer grid via a chosen algorithm."""
    from pathlib import Path

    import numpy as np
    import xarray as xr
    import xarray_regrid  # noqa: F401 — registers the .regrid accessor
    from weather_skills_core.envelope import detect_spatial_dims, detect_time_dim

    lat_dim, lon_dim = detect_spatial_dims(ds, dims)

    if variable:
        if variable not in ds.data_vars:
            raise UsageError(f"variable '{variable}' not in {list(ds.data_vars)}")
        ds = ds[[variable]]

    in_lat_res = _grid_spacing(ds, lat_dim)
    in_lon_res = _grid_spacing(ds, lon_dim)

    # Build the finer target grid from one of the three mutually-exclusive specs.
    if reference_grid is not None:
        ref_grid_path = Path(reference_grid)
        ref_grid_ds = xr.open_zarr(ref_grid_path, consolidated=False)
        for d in (lat_dim, lon_dim):
            if d not in ref_grid_ds.dims:
                raise UsageError(
                    f"--reference-grid missing '{d}' dim (have {list(ref_grid_ds.dims)})."
                )
        new_lat = np.asarray(ref_grid_ds[lat_dim].values)
        new_lon = np.asarray(ref_grid_ds[lon_dim].values)
        ref_lat_res = _grid_spacing(ref_grid_ds, lat_dim)
        ref_lon_res = _grid_spacing(ref_grid_ds, lon_dim)
        if ref_lat_res > in_lat_res or ref_lon_res > in_lon_res:
            raise UsageError(
                f"--reference-grid is coarser than the input "
                f"(input ~{in_lat_res:.4f}°x{in_lon_res:.4f}°, reference "
                f"~{ref_lat_res:.4f}°x{ref_lon_res:.4f}°). Downscaling goes "
                f"finer-or-equal; to coarsen onto a coarser grid use the "
                f"coarsen skill."
            )
        target_desc = f"reference grid {ref_grid_path.name}"
    else:
        if factor is not None:
            lat_spacing = in_lat_res / factor
            lon_spacing = in_lon_res / factor
            target_desc = f"factor {factor}"
        else:
            # --target-resolution: the requested spacing applies to both axes.
            # Require it to be finer-or-equal to BOTH input axis spacings, so a
            # value finer than one axis but coarser than the other is still rejected.
            if target_resolution > in_lat_res or target_resolution > in_lon_res:
                raise UsageError(
                    f"--target-resolution {target_resolution}° is "
                    f"coarser than the input on at least one axis "
                    f"(~{in_lat_res:.4f}°x{in_lon_res:.4f}°). "
                    f"Downscaling goes finer-or-equal; to coarsen onto a coarser "
                    f"grid use the coarsen skill."
                )
            if abs(in_lat_res - in_lon_res) / max(in_lat_res, in_lon_res) > 0.05:
                print(
                    f"Warning: input grid is anisotropic "
                    f"(~{in_lat_res:.4f}°x{in_lon_res:.4f}°); the single "
                    f"--target-resolution {target_resolution}° is applied to "
                    f"both axes.",
                    file=sys.stderr,
                )
            lat_spacing = target_resolution
            lon_spacing = target_resolution
            target_desc = f"target-resolution {target_resolution}°"
        new_lat = _target_coord(ds[lat_dim].values, lat_spacing)
        new_lon = _target_coord(ds[lon_dim].values, lon_spacing)

    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )

    print(
        f"Downscaling {lat_dim},{lon_dim} (algorithm={algorithm}, "
        f"{target_desc}): {ds.sizes[lat_dim]}x{ds.sizes[lon_dim]} -> "
        f"{len(new_lat)}x{len(new_lon)}",
        file=sys.stderr,
    )
    out_ds = ds.regrid.linear(target)

    if algorithm == "q-q":
        ref_path = Path(qq_reference)
        ref_ds = xr.open_zarr(ref_path, consolidated=False)
        # Resolve the axis the mapping runs along: an explicit --time-dim wins,
        # else CF/heuristic detection. Raises naming --time-dim as the remedy.
        time_dim = detect_time_dim(out_ds, time_dim)
        if time_dim not in ref_ds.dims:
            raise UsageError(
                f"--qq-reference has no '{time_dim}' dim (have {list(ref_ds.dims)}); pass --time-dim."
            )
        for d in (lat_dim, lon_dim):
            if d not in ref_ds.dims:
                raise UsageError(f"--qq-reference missing '{d}' dim (have {list(ref_ds.dims)}).")
            if not _coords_match(out_ds[d].values, ref_ds[d].values):
                raise UsageError(
                    f"--qq-reference '{d}' coords do not match the "
                    f"downscaled grid. Reference must be on the post-downscale lat/lon."
                )
        shared = [v for v in out_ds.data_vars if v in ref_ds.data_vars]
        if not shared:
            raise UsageError(
                f"--qq-reference shares no variables with the downscaled "
                f"output (out: {list(out_ds.data_vars)}, ref: {list(ref_ds.data_vars)})."
            )
        for v in out_ds.data_vars:
            if v not in shared:
                print(
                    f"Warning: variable '{v}' not in --qq-reference; passing through unmapped.",
                    file=sys.stderr,
                )
        print(
            f"Q-Q mapping (empirical) {shared} along '{time_dim}': "
            f"model n={out_ds.sizes[time_dim]}, ref n={ref_ds.sizes[time_dim]}",
            file=sys.stderr,
        )
        ref_aligned = ref_ds.assign_coords({lat_dim: out_ds[lat_dim], lon_dim: out_ds[lon_dim]})
        for v in shared:
            mapped = _qmap_dataarray(out_ds[v], ref_aligned[v], time_dim)
            mapped.attrs = dict(out_ds[v].attrs)
            out_ds[v] = mapped

    # Interpolating onto a finer grid replaces the spatial axes but keeps the
    # envelope shape.
    validate_type(out_ds, ds)
    return out_ds, WroteSummary(f"{out_ds.sizes}", replace=True)


if __name__ == "__main__":
    downscale()
