# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cf-xarray",
#   "cftime",
#   # matplotlib<3.10: keep the plot skills on one tested matplotlib
#   "matplotlib>=3.8,<3.10",
#   "nc-time-axis",
#   "numpy",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""Render a multi-input timeseries PNG from weather-skills standard dataset Zarrs."""

import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.standard_utils import dataset_label, pick_time_dim
from weather_skills_core.units import (
    format_units_for_display,
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _y_label(variable, da):
    shown = format_units_for_display(variable_units(da))
    return variable if not shown else f"{variable} [{shown}]"


@weather_skill(
    name="plot-timeseries",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--time-dim",
    default=None,
    help="Time-like dim; default time, then step, then CF time.",
)
@weather_skill.argument(
    "--reduce",
    action="append",
    default=[],
    help="Non-time dim to mean-reduce before plotting. Repeatable.",
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--align-day-of-year",
    action="store_true",
    help="Plot against day-of-year (1-366) instead of absolute date.",
)
def plot_timeseries(ds, variable, time_dim, reduce, title, align_day_of_year, output, **kwargs):
    """Render a multi-input timeseries PNG from weather-skills standard dataset Zarrs."""
    if not isinstance(ds, (list, tuple)):
        datasets = [ds]
    else:
        datasets = list(ds)
    if len(datasets) > 26:
        raise UsageError(f"--input must be passed at most 26 times; got {len(datasets)}.")

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter
    import numpy as np

    variable = variable or auto_variable(datasets[0])
    if variable is None:
        raise UsageError("no usable variable in the first input.")
    for idx, ds in enumerate(datasets):
        if variable not in ds:
            raise UsageError(
                f"variable '{variable}' missing from input {idx + 1}. "
                f"Available: {list(ds.data_vars)}"
            )
    datasets = [
        precip_for_display(to_standard_units(ds, variables=[variable]), variable) for ds in datasets
    ]

    unit_vals = []
    seen_units = {}
    for idx, ds in enumerate(datasets):
        u = variable_units(ds[variable])
        if isinstance(u, str) and u.strip():
            unit_vals.append(u)
            seen_units[dataset_label(ds, f"input {idx + 1}")] = u.strip()
    if unit_vals and any(not units_equal(unit_vals[0], u) for u in unit_vals[1:]):
        detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
        print(
            f"Warning: variable '{variable}' has differing units across the "
            f"overlaid inputs ({detail}). The traces share one y-axis labeled "
            f"with a single unit, so lines in different units are not directly "
            f"comparable in this figure.",
            file=sys.stderr,
        )

    fig, ax = plt.subplots(figsize=(10, 6))
    first_tdim = None
    axis_label = None

    for idx, ds in enumerate(datasets):
        da = ds[variable]
        try:
            tdim = pick_time_dim(da, time_dim)
        except UsageError as exc:
            raise UsageError(f"Error (input {idx + 1}): {exc}", prefix=False) from None

        applicable = [d for d in reduce if d in da.dims]
        if applicable:
            da = da.mean(applicable, keep_attrs=True)

        extras = [d for d in da.dims if d != tdim]
        if extras:
            raise UsageError(
                f"Error (input {idx + 1}): variable '{variable}' still has non-time dims "
                f"{extras} after --reduce. Pass --reduce <dim> for each.",
                prefix=False,
            )

        label = dataset_label(ds, f"input {idx + 1}")
        xlabel = tdim
        if align_day_of_year:
            try:
                xvals = da[tdim].dt.dayofyear.values
            except (TypeError, AttributeError):
                raise UsageError(
                    f"Error (input {idx + 1}): --align-day-of-year needs a calendar-date "
                    f"time axis, but '{tdim}' is not a date axis. Drop the flag or pick "
                    f"a date dim with --time-dim.",
                    prefix=False,
                ) from None
            if len(xvals) > 1 and np.any(np.diff(xvals) < 0):
                print(
                    f"Warning (input {idx + 1}): day-of-year values are non-monotonic; "
                    f"rendering anyway.",
                    file=sys.stderr,
                )
            xlabel = "day of year"
        else:
            xvals = da[tdim].values
            if (
                tdim == "step"
                and np.issubdtype(np.asarray(xvals).dtype, np.timedelta64)
                and "time" in ds.coords
                and ds["time"].ndim == 0
                and np.asarray(ds["time"].values).dtype.kind == "M"
            ):
                xvals = (np.asarray(ds["time"].values) + np.asarray(xvals)).astype("datetime64[ns]")
                xlabel = "valid time"
        ax.plot(xvals, da.values, label=label)

        if first_tdim is None:
            first_tdim = tdim
        if axis_label is None:
            axis_label = xlabel

    ax.set_xlabel(axis_label or first_tdim or "time")
    ax.set_ylabel(_y_label(variable, datasets[0][variable]))
    if title:
        ax.set_title(title)
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.5)

    fig.autofmt_xdate()
    fig.tight_layout()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_timeseries()
