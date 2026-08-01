# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
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

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.standard_utils import dataset_label, pick_time_dim
from weather_skills_core.units import to_standard_units, units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.14"

@weather_skill(
    name="plot-timeseries",
    version=_SKILL_VERSION,
    inputs=["any+"],
    outputs=["figure"],
    allow_precip_totals=True,
)
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
def plot_timeseries(datasets, variable, time_dim, reduce, title, align_day_of_year, output, **kwargs):
    """Render a multi-input timeseries PNG from weather-skills standard dataset Zarrs."""
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
    datasets = [to_standard_units(ds, variables=[variable]) for ds in datasets]

    unit_vals = []
    seen_units = {}
    for idx, ds in enumerate(datasets):
        u = ds[variable].attrs.get("units")
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
    units = None
    first_tdim = None

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
        else:
            xvals = da[tdim].values
        ax.plot(xvals, da.values, label=label)

        if units is None:
            units = da.attrs.get("units")
        if first_tdim is None:
            first_tdim = tdim

    ax.set_xlabel("day of year" if align_day_of_year else (first_tdim or "time"))
    ax.set_ylabel(variable if not units else f"{variable} [{units}]")
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
