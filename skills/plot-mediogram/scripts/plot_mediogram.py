# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "cf-xarray",
#   "xarray",
#   "zarr",
#   "matplotlib",
#   "numpy",
# ]
# ///
"""ECMWF-style mediogram: forecast vs m-climate ensemble distributions at a point."""

import argparse
import sys
from pathlib import Path


def _cf_dim(obj, cf_name):
    try:
        return obj.cf[cf_name].name
    except KeyError:
        return None


def _select_point(da, lat, lon):
    lat_dim = _cf_dim(da, "latitude")
    lon_dim = _cf_dim(da, "longitude")
    if lat_dim is None or lon_dim is None:
        raise ValueError(f"Could not identify latitude/longitude in dims {list(da.dims)}.")
    return da.sel({lat_dim: lat, lon_dim: lon}, method="nearest")


def _outer_stats(values):
    import numpy as np

    return {
        "whislo": float(np.percentile(values, 25)),
        "q1": float(np.percentile(values, 25)),
        "med": float(np.percentile(values, 50)),
        "q3": float(np.percentile(values, 75)),
        "whishi": float(np.percentile(values, 75)),
        "fliers": [],
    }


def _inner_stats(values):
    import numpy as np

    return {
        "whislo": float(np.percentile(values, 0)),
        "q1": float(np.percentile(values, 10)),
        "med": float(np.percentile(values, 50)),
        "q3": float(np.percentile(values, 90)),
        "whishi": float(np.percentile(values, 100)),
        "fliers": [],
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--forecast", required=True, help="Forecast Zarr (number × step × spatial)")
    p.add_argument("--mclimate", required=True, help="M-climate Zarr (same schema)")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--variable", "-v")
    p.add_argument("--title")
    args = p.parse_args()

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import numpy as np
    import xarray as xr
    from matplotlib.patches import Patch

    for pth in (args.forecast, args.mclimate):
        if not Path(pth).exists():
            print(f"Error: {pth} not found.", file=sys.stderr)
            sys.exit(2)

    ds_fc = xr.open_zarr(args.forecast, consolidated=False)
    ds_mc = xr.open_zarr(args.mclimate, consolidated=False)

    variable = args.variable or (list(ds_fc.data_vars)[0] if ds_fc.data_vars else None)
    if variable is None or variable not in ds_fc or variable not in ds_mc:
        print(
            f"Error: variable '{variable}' must exist in both inputs. "
            f"forecast: {list(ds_fc.data_vars)}  mclimate: {list(ds_mc.data_vars)}",
            file=sys.stderr,
        )
        sys.exit(2)

    da_fc = ds_fc[variable]
    da_mc = ds_mc[variable]

    for label, da in (("forecast", da_fc), ("mclimate", da_mc)):
        if "number" not in da.dims or "step" not in da.dims:
            print(
                f"Error: {label} input requires 'number' and 'step' dims; got {list(da.dims)}.",
                file=sys.stderr,
            )
            sys.exit(2)

    pt_fc = _select_point(da_fc, args.lat, args.lon)
    pt_mc = _select_point(da_mc, args.lat, args.lon)

    n_steps = min(pt_fc.sizes["step"], pt_mc.sizes["step"], 6)
    if n_steps < 1:
        print("Error: no overlapping steps to plot.", file=sys.stderr)
        sys.exit(1)

    pt_fc = pt_fc.isel(step=slice(0, n_steps)).transpose("number", "step")
    pt_mc = pt_mc.isel(step=slice(0, n_steps)).transpose("number", "step")
    fc = pt_fc.values
    mc = pt_mc.values

    time_steps = np.arange(n_steps)
    ensemble_mean = np.mean(fc, axis=0)

    fig, ax = plt.subplots(figsize=(10, 5))

    fc_outer = [_outer_stats(fc[:, i]) for i in range(n_steps)]
    mc_outer = [_outer_stats(mc[:, i]) for i in range(n_steps)]
    fc_inner = [_inner_stats(fc[:, i]) for i in range(n_steps)]
    mc_inner = [_inner_stats(mc[:, i]) for i in range(n_steps)]

    pos_fc = time_steps - 0.2
    pos_mc = time_steps + 0.2

    ax.bxp(
        fc_outer,
        positions=pos_fc,
        widths=0.4,
        showfliers=False,
        patch_artist=True,
        boxprops=dict(facecolor="cyan", alpha=1),
        medianprops=dict(color="black", linewidth=1.5),
        whiskerprops=dict(color="gray", linewidth=2),
        capprops=dict(color="black", linewidth=1, alpha=0),
    )
    ax.bxp(
        mc_outer,
        positions=pos_mc,
        widths=0.4,
        showfliers=False,
        patch_artist=True,
        boxprops=dict(facecolor="red", alpha=1),
        medianprops=dict(color="black", linewidth=1.5),
        whiskerprops=dict(color="gray", linewidth=2),
        capprops=dict(color="black", linewidth=1, alpha=0),
    )

    ax.bxp(
        fc_inner,
        positions=pos_fc,
        widths=0.2,
        showfliers=False,
        patch_artist=True,
        boxprops=dict(facecolor="cyan", alpha=1),
        medianprops=dict(color="black", linewidth=1.5),
        whiskerprops=dict(color="black", linewidth=1),
        capprops=dict(color="gray", linewidth=1, alpha=0),
    )
    ax.bxp(
        mc_inner,
        positions=pos_mc,
        widths=0.2,
        showfliers=False,
        patch_artist=True,
        boxprops=dict(facecolor="red", alpha=1),
        medianprops=dict(color="black", linewidth=1.5),
        whiskerprops=dict(color="black", linewidth=1),
        capprops=dict(color="gray", linewidth=1, alpha=0),
    )

    ax.plot(time_steps, ensemble_mean, color="black", linewidth=1.2)

    ax.set_xticks(time_steps)
    ax.set_xticklabels([f"T+{t + 1}" for t in time_steps])
    ax.set_xlabel("Forecast step")
    ax.set_ylabel(variable)
    ax.set_title(args.title or f"Mediogram: {variable} at lat={args.lat}, lon={args.lon}")
    ax.grid(True, linestyle="--", alpha=0.6)

    handles = [
        Patch(facecolor="cyan", edgecolor="black", label="forecast"),
        Patch(facecolor="red", edgecolor="black", label="m-climate"),
    ]
    ax.legend(handles=handles)

    fig.tight_layout()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
