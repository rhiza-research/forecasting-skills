# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "xarray",
#   "xarray-regrid",
#   "zarr",
#   "numpy",
# ]
# ///
"""Linear regridding for Rhiza Envelope Zarr stores."""

import argparse
import shutil
import sys
from pathlib import Path


def _grid_spacing(ds, dim):
    import numpy as np

    coord = ds[dim].values
    if coord.size < 2:
        raise ValueError(f"Cannot infer spacing for dim '{dim}' with size {coord.size}")
    return float(abs(np.median(np.diff(coord))))


def _factor_from_target(ds, lat_dim, lon_dim, target_res):
    lat_res = _grid_spacing(ds, lat_dim)
    lon_res = _grid_spacing(ds, lon_dim)
    if abs(lat_res - lon_res) / max(lat_res, lon_res) > 0.05:
        print(
            f"Warning: anisotropic input grid ({lat_res:.4f}° vs {lon_res:.4f}°); "
            f"using mean for factor.",
            file=sys.stderr,
        )
    mean_res = (lat_res + lon_res) / 2
    factor = round(target_res / mean_res)
    if factor < 2:
        print(
            f"Error: --target-resolution {target_res}° is not coarser than input "
            f"grid (~{mean_res:.4f}°).",
            file=sys.stderr,
        )
        sys.exit(2)
    return factor


def _target_coord(coord, new_spacing):
    import numpy as np

    vmin = float(np.min(coord))
    vmax = float(np.max(coord))
    n = int(np.floor((vmax - vmin) / new_spacing)) + 1
    target = vmin + new_spacing * np.arange(n)
    if coord[0] > coord[-1]:
        target = target[::-1]
    return target


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--factor", "-f", type=int)
    grp.add_argument(
        "--target-resolution", type=float, help="Target grid spacing in degrees"
    )
    p.add_argument("--dims", help="Override as LAT,LON dim names")
    p.add_argument("--variable", "-v", help="Restrict to a single data variable")
    args = p.parse_args()

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import xarray as xr
    import xarray_regrid  # noqa: F401 — registers the .regrid accessor

    src = Path(args.input)
    if not src.exists():
        print(f"Error: input {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if args.dims:
        lat_dim, lon_dim = [s.strip() for s in args.dims.split(",")]
        if lat_dim not in ds.dims or lon_dim not in ds.dims:
            print(
                f"Error: --dims names not in dataset dims {list(ds.dims)}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        try:
            lat_dim = ds.cf["latitude"].name
            lon_dim = ds.cf["longitude"].name
        except KeyError:
            print(
                f"Error: could not identify lat/lon coords via CF metadata or name "
                f"heuristics in {list(ds.coords)}. Pass --dims to override.",
                file=sys.stderr,
            )
            sys.exit(2)

    factor = (
        args.factor
        if args.factor is not None
        else _factor_from_target(ds, lat_dim, lon_dim, args.target_resolution)
    )
    if factor < 2:
        print("Error: factor must be >= 2.", file=sys.stderr)
        sys.exit(2)

    if args.variable:
        if args.variable not in ds.data_vars:
            print(
                f"Error: variable '{args.variable}' not in {list(ds.data_vars)}",
                file=sys.stderr,
            )
            sys.exit(2)
        ds = ds[[args.variable]]

    new_lat = _target_coord(ds[lat_dim].values, factor * _grid_spacing(ds, lat_dim))
    new_lon = _target_coord(ds[lon_dim].values, factor * _grid_spacing(ds, lon_dim))
    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )

    print(
        f"Regridding {lat_dim},{lon_dim} by factor {factor} (linear): "
        f"{ds.sizes[lat_dim]}x{ds.sizes[lon_dim]} -> {len(new_lat)}x{len(new_lon)}",
        file=sys.stderr,
    )
    out_ds = ds.regrid.linear(target)

    out_ds.attrs = {
        **ds.attrs,
        "rhiza_downscale_factor": factor,
        "rhiza_downscale_method": "linear",
    }
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(f"Wrote: {args.output} ({out_ds.sizes})", file=sys.stderr)


if __name__ == "__main__":
    main()
