# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "cf-xarray",
#   "cftime",
#   "xarray",
#   "zarr",
#   # matplotlib<3.10: keep the plot skills on one tested matplotlib
#   "matplotlib>=3.8,<3.10",
#   "numpy",
# ]
# ///
"""ECMWF-style mediogram: forecast vs m-climate ensemble distributions at a point."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.9"


def _cf_dim(obj, cf_name):
    try:
        return obj.cf[cf_name].name
    except KeyError:
        return None


def _hash_zarr(zarr_path: Path) -> str:
    """Stable content hash of a zarr's stored bytes. Walks the zarr dir
    deterministically and hashes relative-path bytes + each file's
    content. Returns sha256 hex digest."""
    h = hashlib.sha256()
    for p in sorted(zarr_path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(zarr_path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _load_history(zarr_path: Path) -> list:
    try:
        import xarray as xr

        with xr.open_zarr(zarr_path, consolidated=False) as ds:
            # compatibility read for the rhiza_ attr prefix; scheduled for removal
            raw = ds.attrs.get("weather_skills_history") or ds.attrs.get("rhiza_history")
    except FileNotFoundError:
        # A not-yet-existing output read during a cache check is a silent miss.
        return []
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list):
        # A present-but-non-array value is malformed under the weather_skills_history
        # contract; treat it as no history and flag it on stderr.
        print(
            f"ignoring malformed weather_skills_history on {zarr_path}; "
            "run `provenance --check` for details",
            file=sys.stderr,
        )
        return []
    return parsed


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
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"skill version: {_SKILL_VERSION}",
    )
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

    lat_dim = _cf_dim(pt_fc, "latitude")
    lon_dim = _cf_dim(pt_fc, "longitude")
    snapped_lat = float(pt_fc[lat_dim].values) if lat_dim else args.lat
    snapped_lon = float(pt_fc[lon_dim].values) if lon_dim else args.lon

    time_steps = np.arange(n_steps)
    ensemble_mean = np.mean(fc, axis=0)

    fig, ax = plt.subplots(figsize=(10, 5))

    fc_outer = [_outer_stats(fc[:, i]) for i in range(n_steps)]
    mc_outer = [_outer_stats(mc[:, i]) for i in range(n_steps)]
    fc_inner = [_inner_stats(fc[:, i]) for i in range(n_steps)]
    mc_inner = [_inner_stats(mc[:, i]) for i in range(n_steps)]

    pos_fc = time_steps - 0.2
    pos_mc = time_steps + 0.2

    # Reference draws the extreme/inner box first, then the IQR/outer box on top
    # at width 0.4 with visible black caps — the caps appear as horizontal lines
    # at p25 and p75 since whiskers are zero-length.
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

    ax.bxp(
        fc_outer,
        positions=pos_fc,
        widths=0.4,
        showfliers=False,
        patch_artist=True,
        boxprops=dict(facecolor="cyan", alpha=1),
        medianprops=dict(color="black", linewidth=1.5),
        whiskerprops=dict(color="gray", linewidth=2),
        capprops=dict(color="black", linewidth=1),
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
        capprops=dict(color="black", linewidth=1),
    )

    ax.plot(time_steps, ensemble_mean, color="black", linewidth=1.2)

    ax.set_xticks(time_steps)
    ax.set_xticklabels([f"T+{t + 1}" for t in time_steps])
    ax.set_xlabel("Forecast step")
    ax.set_ylabel(variable)
    ax.set_title(args.title or f"Mediogram: {variable} at lat={snapped_lat:g}, lon={snapped_lon:g}")
    ax.grid(True, linestyle="--", alpha=0.6)

    handles = [
        Patch(facecolor="cyan", edgecolor="black", label="forecast"),
        Patch(facecolor="red", edgecolor="black", label="m-climate"),
    ]
    ax.legend(handles=handles)

    fig.tight_layout()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    src_forecast = Path(args.forecast)
    src_mclimate = Path(args.mclimate)
    upstream_forecast = _load_history(src_forecast)
    upstream_mclimate = _load_history(src_mclimate)
    shared_args = {
        k: v for k, v in vars(args).items() if k not in {"forecast", "mclimate", "output"}
    }
    version = _SKILL_VERSION
    mediogram_entry_forecast = {
        "skill": "plot-mediogram",
        "version": version,
        "args": shared_args,
        "input": {"basename": src_forecast.name, "hash": _hash_zarr(src_forecast)},
    }
    mediogram_entry_mclimate = {
        "skill": "plot-mediogram",
        "version": version,
        "args": shared_args,
        "input": {"basename": src_mclimate.name, "hash": _hash_zarr(src_mclimate)},
    }
    if not upstream_forecast:
        print(
            f"Warning: no upstream weather_skills_history on {src_forecast.name}; "
            "embedding plot-mediogram step alone.",
            file=sys.stderr,
        )
    if not upstream_mclimate:
        print(
            f"Warning: no upstream weather_skills_history on {src_mclimate.name}; "
            "embedding plot-mediogram step alone.",
            file=sys.stderr,
        )
    fig.savefig(
        out,
        dpi=150,
        metadata={
            "weather_skills_history_forecast": json.dumps(
                upstream_forecast + [mediogram_entry_forecast], sort_keys=True
            ),
            "weather_skills_history_mclimate": json.dumps(
                upstream_mclimate + [mediogram_entry_mclimate], sort_keys=True
            ),
            "Software": "forecasting-skills",
        },
    )
    plt.close(fig)
    print(f"Wrote: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
