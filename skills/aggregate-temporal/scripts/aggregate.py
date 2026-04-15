# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pandas",
# ]
# ///
"""Temporal aggregation for Rhiza Envelope Zarr stores.

Supports a `time` dim (wall-clock) or a `step` dim (forecast lead time).
For `time`, uses xarray.resample. For `step`, rolls fixed-length windows
expressed as timedelta64 and aggregates each.
"""

import argparse
import shutil
import sys
from pathlib import Path

PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10}
RESAMPLE_FREQ = {"daily": "1D", "weekly": "7D", "dekadal": "10D", "monthly": "MS"}


def _reduce(grouped, method):
    return {
        "sum": grouped.sum,
        "mean": grouped.mean,
        "max": grouped.max,
        "min": grouped.min,
    }[method]()


def _aggregate_step(ds, period, method):
    import numpy as np
    import pandas as pd

    if period == "monthly":
        print(
            "Error: monthly aggregation is not defined for a forecast step dim.",
            file=sys.stderr,
        )
        sys.exit(2)
    days = PERIOD_DAYS[period]
    window = pd.Timedelta(days=days).to_timedelta64()
    steps = ds["step"].values
    if steps.dtype.kind != "m":
        print(
            f"Error: 'step' dim must be timedelta64, got {steps.dtype}", file=sys.stderr
        )
        sys.exit(2)
    max_step = steps.max()
    edges = np.arange(0, max_step + window, window, dtype=steps.dtype)
    chunks, labels = [], []
    for left in edges[:-1]:
        right = left + window
        mask = (steps >= left) & (steps < right)
        if not mask.any():
            continue
        sel = ds.isel(step=np.where(mask)[0])
        chunks.append(
            _reduce(sel, method=method).drop_vars("step", errors="ignore")
            if "step" in sel.dims
            else _reduce(sel, method=method)
        )
        labels.append(left + window / 2)
    import xarray as xr

    return xr.concat(chunks, dim="step").assign_coords(step=labels)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument(
        "--period", required=True, choices=["daily", "weekly", "dekadal", "monthly"]
    )
    p.add_argument("--method", default="sum", choices=["sum", "mean", "max", "min"])
    p.add_argument("--time-dim")
    args = p.parse_args()

    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import xarray as xr

    src = Path(args.input)
    if not src.exists():
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(2)
    ds = xr.open_zarr(src, consolidated=False)

    if args.time_dim:
        dim = args.time_dim
        if dim not in ds.dims:
            print(
                f"Error: --time-dim '{dim}' not in dims {list(ds.dims)}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        # CF "T" axis first (finds wall-clock time even when named unusually),
        # then `step` (forecast lead time — timedelta64, not CF T).
        try:
            dim = ds.cf["time"].name
        except KeyError:
            dim = "step" if "step" in ds.dims else None
        if dim is None or dim not in ds.dims:
            print(
                f"Error: no time/step dim identified in {list(ds.dims)}. "
                f"Pass --time-dim to override.",
                file=sys.stderr,
            )
            sys.exit(2)

    print(
        f"Aggregating dim={dim} period={args.period} method={args.method}",
        file=sys.stderr,
    )

    if dim == "step":
        out_ds = _aggregate_step(ds, args.period, args.method)
    else:
        resampled = ds.resample({dim: RESAMPLE_FREQ[args.period]})
        out_ds = _reduce(resampled, args.method)

    out_ds.attrs = {**ds.attrs, "rhiza_aggregation": f"{args.period}-{args.method}"}
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
