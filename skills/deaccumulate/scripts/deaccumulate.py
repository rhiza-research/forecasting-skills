# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray>=0.11",
#   "xarray>=2026.4",
#   "zarr>=3.2",
#   "numpy>=2.4",
# ]
# ///
"""Deaccumulate a cumulative-since-init variable along the forecast step axis.

Some forecast variables (e.g. ECMWF S2S ``tp``, surface radiation, evaporation,
SWE) are stored as values accumulated from the forecast initialization time.
This skill converts those to per-step diffs: ``out[i] = arr[i+1] - arr[i]``,
clipped at zero. The output ``step`` coord drops the first input step, so the
resulting axis labels each value with the end of the period it covers.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path


def _upstream_inputs(zarr_path: Path) -> str | None:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            return ds.attrs.get("rhiza_inputs")
    except Exception:
        return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--variable",
        "-v",
        help="Variable to deaccumulate. Required if the input has multiple data vars.",
    )
    args = p.parse_args()

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import numpy as np
    import xarray as xr

    src = Path(args.input)
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if "step" not in ds.dims:
        print(
            f"Error: input has no 'step' dim; got dims {list(ds.dims)}.",
            file=sys.stderr,
        )
        sys.exit(2)

    data_vars = list(ds.data_vars)
    if args.variable:
        if args.variable not in ds.data_vars:
            print(
                f"Error: variable '{args.variable}' not in data_vars {data_vars}.",
                file=sys.stderr,
            )
            sys.exit(2)
        variable = args.variable
    elif len(data_vars) == 1:
        variable = data_vars[0]
    else:
        print(
            f"Error: input has multiple data vars {data_vars}; specify --variable.",
            file=sys.stderr,
        )
        sys.exit(2)

    da = ds[variable]
    if da.sizes["step"] < 2:
        print(
            f"Error: 'step' dim has length {da.sizes['step']}; need at least 2 to diff.",
            file=sys.stderr,
        )
        sys.exit(2)

    diffed = da.isel(step=slice(1, None)).copy(
        data=np.clip(
            da.isel(step=slice(1, None)).values - da.isel(step=slice(0, -1)).values,
            a_min=0,
            a_max=None,
        )
    )
    diffed.attrs = dict(da.attrs)

    out_ds = ds.drop_vars(variable).isel(step=slice(1, None))
    out_ds[variable] = diffed
    inputs = {
        "variable": variable,
        "input": _upstream_inputs(src),
    }
    out_ds.attrs = {
        **ds.attrs,
        "rhiza_deaccumulated": "true",
        "rhiza_inputs": json.dumps(inputs, sort_keys=True),
    }
    for v in out_ds.variables:
        out_ds[v].encoding = {}

    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_ds.to_zarr(out, mode="w", consolidated=True)
    print(
        f"Wrote: {args.output} (variable={variable}, step length {da.sizes['step']} -> {out_ds.sizes['step']})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
