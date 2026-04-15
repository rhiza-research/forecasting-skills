# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "xarray",
#   "zarr",
#   "numpy",
# ]
# ///
"""Concatenate Rhiza Envelope Zarr stores along a named dim."""

import argparse
import shutil
import sys
from pathlib import Path


def _coerce(values):
    out = []
    for v in values:
        try:
            out.append(int(v))
        except ValueError:
            try:
                out.append(float(v))
            except ValueError:
                out.append(v)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", required=True, help="Comma-separated Zarr paths")
    p.add_argument("--dim", required=True)
    p.add_argument("--coords", help="Comma-separated coord values for the new dim")
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    import xarray as xr

    paths = [Path(s.strip()) for s in args.inputs.split(",") if s.strip()]
    if len(paths) < 2:
        print("Error: need at least 2 inputs.", file=sys.stderr)
        sys.exit(2)
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"Error: missing inputs: {missing}", file=sys.stderr)
        sys.exit(2)

    dss = [xr.open_zarr(p, consolidated=False) for p in paths]
    dim_on_inputs = all(args.dim in ds.dims for ds in dss)

    if not dim_on_inputs:
        if args.coords:
            coord_vals = _coerce([c.strip() for c in args.coords.split(",")])
            if len(coord_vals) != len(dss):
                print(
                    f"Error: --coords len {len(coord_vals)} != inputs {len(dss)}",
                    file=sys.stderr,
                )
                sys.exit(2)
            dss = [d.expand_dims({args.dim: [v]}) for d, v in zip(dss, coord_vals)]
        else:
            dss = [d.expand_dims(args.dim) for d in dss]

    out_ds = xr.concat(dss, dim=args.dim)
    out_ds.attrs = dict(dss[0].attrs)
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
