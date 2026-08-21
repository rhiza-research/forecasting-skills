# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cf-xarray",
#   "cftime",
#   "xarray",
#   "zarr",
#   # matplotlib<3.10: keep the plot skills on one tested matplotlib
#   "matplotlib>=3.8,<3.10",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""ECMWF-style mediogram: forecast vs m-climate ensemble distributions at a point."""

from pathlib import Path

from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable, cf_dim
from weather_skills_core.units import (
    format_units_for_display,
    precip_for_display,
    to_standard_units,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _select_point(da, lat, lon):
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
    if lat_dim is None or lon_dim is None:
        raise ValueError(f"Could not identify latitude/longitude in dims {list(da.dims)}.")
    return da.sel({lat_dim: lat, lon_dim: lon}, method="nearest")


def _bxp_stats(values, lo, q1, q3, hi):
    import numpy as np

    return {
        "whislo": float(np.percentile(values, lo)),
        "q1": float(np.percentile(values, q1)),
        "med": float(np.percentile(values, 50)),
        "q3": float(np.percentile(values, q3)),
        "whishi": float(np.percentile(values, hi)),
        "fliers": [],
    }


def _draw_bxp(ax, stats, positions, width, facecolor, whisker_lw, cap_alpha=1):
    ax.bxp(
        stats,
        positions=positions,
        widths=width,
        showfliers=False,
        patch_artist=True,
        boxprops={"facecolor": facecolor, "alpha": 1},
        medianprops={"color": "black", "linewidth": 1.5},
        whiskerprops={"color": "black" if whisker_lw <= 1 else "gray", "linewidth": whisker_lw},
        capprops={
            "color": "gray" if cap_alpha == 0 else "black",
            "linewidth": 1,
            "alpha": cap_alpha,
        },
    )


@weather_skill(
    name="plot-mediogram",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lat", type=float, required=True, help="Point latitude.")
@weather_skill.argument("--lon", type=float, required=True, help="Point longitude.")
@weather_skill.argument("--title", default=None, help="Optional plot title.")
def plot_mediogram(ds, variable, lat, lon, title, output, **kwargs):
    """ECMWF-style mediogram: forecast vs m-climate ensemble distributions at a point."""
    if len(ds) != 2:
        raise UsageError(f"expected exactly two --input paths, got {len(ds)}")
    ds_fc, ds_mc = ds
    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch

    variable = variable or auto_variable(ds_fc)
    if variable is None or variable not in ds_fc or variable not in ds_mc:
        raise UsageError(
            f"variable '{variable}' must exist in both inputs. "
            f"forecast: {list(ds_fc.data_vars)}  mclimate: {list(ds_mc.data_vars)}"
        )

    ds_fc = precip_for_display(to_standard_units(ds_fc, variables=[variable]), variable)
    ds_mc = precip_for_display(to_standard_units(ds_mc, variables=[variable]), variable)
    da_fc = ds_fc[variable]
    da_mc = ds_mc[variable]

    for label, da in (("forecast", da_fc), ("mclimate", da_mc)):
        if "number" not in da.dims or "step" not in da.dims:
            raise UsageError(
                f"{label} input requires 'number' and 'step' dims; got {list(da.dims)}."
            )

    pt_fc = _select_point(da_fc, lat, lon)
    pt_mc = _select_point(da_mc, lat, lon)

    n_steps = min(pt_fc.sizes["step"], pt_mc.sizes["step"], 6)
    if n_steps < 1:
        raise DataError("no overlapping steps to plot.")

    pt_fc = pt_fc.isel(step=slice(0, n_steps)).transpose("number", "step")
    pt_mc = pt_mc.isel(step=slice(0, n_steps)).transpose("number", "step")
    fc = pt_fc.values
    mc = pt_mc.values

    lat_dim = cf_dim(pt_fc, "latitude")
    lon_dim = cf_dim(pt_fc, "longitude")
    snapped_lat = float(pt_fc[lat_dim].values) if lat_dim else lat
    snapped_lon = float(pt_fc[lon_dim].values) if lon_dim else lon

    time_steps = np.arange(n_steps)
    fig, ax = plt.subplots(figsize=(10, 5))

    fc_outer = [_bxp_stats(fc[:, i], 25, 25, 75, 75) for i in range(n_steps)]
    mc_outer = [_bxp_stats(mc[:, i], 25, 25, 75, 75) for i in range(n_steps)]
    fc_inner = [_bxp_stats(fc[:, i], 0, 10, 90, 100) for i in range(n_steps)]
    mc_inner = [_bxp_stats(mc[:, i], 0, 10, 90, 100) for i in range(n_steps)]

    pos_fc = time_steps - 0.2
    pos_mc = time_steps + 0.2
    _draw_bxp(ax, fc_inner, pos_fc, 0.2, "cyan", 1, cap_alpha=0)
    _draw_bxp(ax, mc_inner, pos_mc, 0.2, "red", 1, cap_alpha=0)
    _draw_bxp(ax, fc_outer, pos_fc, 0.4, "cyan", 2)
    _draw_bxp(ax, mc_outer, pos_mc, 0.4, "red", 2)

    ax.plot(time_steps, np.mean(fc, axis=0), color="black", linewidth=1.2)
    ax.set_xticks(time_steps)
    step_vals = np.asarray(pt_fc["step"].values)
    tick_labels = []
    for value in step_vals:
        arr = np.asarray(value)
        if arr.dtype.kind == "m":
            tick_labels.append(f"+{int(arr.astype('timedelta64[D]').astype(int))}d")
        else:
            tick_labels.append(str(value))
    ax.set_xticklabels(tick_labels)
    ax.set_xlabel("Forecast step")
    shown = format_units_for_display(variable_units(pt_fc))
    ax.set_ylabel(variable if not shown else f"{variable} [{shown}]")
    ax.set_title(title or f"Mediogram: {variable} at lat={snapped_lat:g}, lon={snapped_lon:g}")
    ax.grid(True, linestyle="--", alpha=0.6)
    ax.legend(
        handles=[
            Patch(facecolor="cyan", edgecolor="black", label="forecast"),
            Patch(facecolor="red", edgecolor="black", label="m-climate"),
        ]
    )

    fig.tight_layout()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_mediogram()
