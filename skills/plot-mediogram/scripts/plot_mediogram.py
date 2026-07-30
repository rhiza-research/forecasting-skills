# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@cursor/simplify-weather-skill-decorator",
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

from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.envelope import auto_variable, cf_dim

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"

def _select_point(da, lat, lon):
    lat_dim = cf_dim(da, "latitude")
    lon_dim = cf_dim(da, "longitude")
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

@weather_skill(
    "plot-mediogram",
    _SKILL_VERSION,
    inputs=["any", "any"],
    outputs=["visualization"]
)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lat", type=float, required=True, help="Point latitude.")
@weather_skill.argument("--lon", type=float, required=True, help="Point longitude.")
@weather_skill.argument("--title", default=None, help="Optional plot title.")
def plot_mediogram(ds_fc, ds_mc, variable, lat, lon, title, output, **kwargs):
    """ECMWF-style mediogram: forecast vs m-climate ensemble distributions at a point."""
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
    ensemble_mean = np.mean(fc, axis=0)

    fig, ax = plt.subplots(figsize=(10, 5))

    fc_outer = [_outer_stats(fc[:, i]) for i in range(n_steps)]
    mc_outer = [_outer_stats(mc[:, i]) for i in range(n_steps)]
    fc_inner = [_inner_stats(fc[:, i]) for i in range(n_steps)]
    mc_inner = [_inner_stats(mc[:, i]) for i in range(n_steps)]

    pos_fc = time_steps - 0.2
    pos_mc = time_steps + 0.2

    ax.bxp(
        fc_inner,
        positions=pos_fc,
        widths=0.2,
        showfliers=False,
        patch_artist=True,
        boxprops={"facecolor": "cyan", "alpha": 1},
        medianprops={"color": "black", "linewidth": 1.5},
        whiskerprops={"color": "black", "linewidth": 1},
        capprops={"color": "gray", "linewidth": 1, "alpha": 0},
    )
    ax.bxp(
        mc_inner,
        positions=pos_mc,
        widths=0.2,
        showfliers=False,
        patch_artist=True,
        boxprops={"facecolor": "red", "alpha": 1},
        medianprops={"color": "black", "linewidth": 1.5},
        whiskerprops={"color": "black", "linewidth": 1},
        capprops={"color": "gray", "linewidth": 1, "alpha": 0},
    )

    ax.bxp(
        fc_outer,
        positions=pos_fc,
        widths=0.4,
        showfliers=False,
        patch_artist=True,
        boxprops={"facecolor": "cyan", "alpha": 1},
        medianprops={"color": "black", "linewidth": 1.5},
        whiskerprops={"color": "gray", "linewidth": 2},
        capprops={"color": "black", "linewidth": 1},
    )
    ax.bxp(
        mc_outer,
        positions=pos_mc,
        widths=0.4,
        showfliers=False,
        patch_artist=True,
        boxprops={"facecolor": "red", "alpha": 1},
        medianprops={"color": "black", "linewidth": 1.5},
        whiskerprops={"color": "gray", "linewidth": 2},
        capprops={"color": "black", "linewidth": 1},
    )

    ax.plot(time_steps, ensemble_mean, color="black", linewidth=1.2)

    ax.set_xticks(time_steps)
    ax.set_xticklabels([f"T+{t + 1}" for t in time_steps])
    ax.set_xlabel("Forecast step")
    ax.set_ylabel(variable)
    ax.set_title(title or f"Mediogram: {variable} at lat={snapped_lat:g}, lon={snapped_lon:g}")
    ax.grid(True, linestyle="--", alpha=0.6)

    handles = [
        Patch(facecolor="cyan", edgecolor="black", label="forecast"),
        Patch(facecolor="red", edgecolor="black", label="m-climate"),
    ]
    ax.legend(handles=handles)

    fig.tight_layout()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output

if __name__ == "__main__":
    plot_mediogram()
