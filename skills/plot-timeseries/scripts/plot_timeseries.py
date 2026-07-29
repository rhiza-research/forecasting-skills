# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@cursor/simplify-weather-skill-decorator",
#   "cf-xarray",
#   "cftime",
#   # matplotlib<3.10: keep the plot skills on one tested matplotlib
#   "matplotlib>=3.8,<3.10",
#   "nc-time-axis",
#   "numpy",
#   "xarray",
#   "zarr",
# ]
# ///
"""Render a multi-input timeseries PNG from one or more weather-skills envelope Zarrs.

Each input contributes one 1D line trace on a shared set of axes, plotted
against its time-like coord. Inputs whose selected variable is not already
1D must list the dims to reduce via repeated --reduce flags; reductions are
mean-only and explicit (no silent averaging).
"""

import sys
from pathlib import Path

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.envelope import auto_variable, cf_dim

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.14"


def _pick_time_dim(da, override):
    if override:
        if override not in da.dims:
            raise ValueError(f"--time-dim '{override}' not in dims {list(da.dims)}.")
        return override
    if "time" in da.dims:
        return "time"
    if "step" in da.dims:
        return "step"
    cf = cf_dim(da, "time")
    if cf and cf in da.dims:
        return cf
    raise ValueError(
        f"Could not identify a time-like dim in {list(da.dims)}; pass --time-dim explicitly."
    )


def _dataset_label(ds, index):
    src = ds.attrs.get("weather_skills_source")
    if isinstance(src, str) and src.strip():
        return Path(src).stem
    return f"input {index + 1}"


@weather_skill(
    "plot-timeseries",
    _SKILL_VERSION,
    inputs=["any+"],
    outputs=["visualization"],
    variable="single_optional",
    extra_args=[
        (
            ("--time-dim",),
            {
                "default": None,
                "help": "Name of the time-like dim. When omitted, time, then step, "
                "then the cf-xarray-identified time axis.",
            },
        ),
        (
            ("--reduce",),
            {
                "action": "append",
                "default": [],
                "help": "Name of a non-time dim to mean-reduce before plotting. Repeatable.",
            },
        ),
        (("--title",), {"default": None, "help": "Optional figure title."}),
        (
            ("--align-day-of-year",),
            {
                "action": "store_true",
                "help": (
                    "Plot each trace against day-of-year (1-366) instead of its absolute "
                    "date, so inputs from different years overlay on a shared x-axis. "
                    "Requires a calendar-date time axis (errors on a non-date axis such "
                    "as a forecast 'step' timedelta)."
                ),
            },
        ),
    ],
)
def plot_timeseries(datasets, variable, time_dim, reduce, title, align_day_of_year, output):
    """Render a multi-input timeseries PNG from one or more weather-skills envelope Zarrs.

    Each input contributes one 1D line trace on a shared set of axes, plotted
    against its time-like coord. Inputs whose selected variable is not already
    1D must list the dims to reduce via repeated --reduce flags; reductions are
    mean-only and explicit (no silent averaging).
    """
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

    seen_units = {}
    for idx, ds in enumerate(datasets):
        if variable not in ds:
            continue
        u = ds[variable].attrs.get("units")
        if isinstance(u, str):
            seen_units[_dataset_label(ds, idx)] = u.strip()
    if len(set(seen_units.values())) > 1:
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
            tdim = _pick_time_dim(da, time_dim)
        except ValueError as exc:
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

        label = _dataset_label(ds, idx)
        if align_day_of_year:
            try:
                xvals = da[tdim].dt.dayofyear.values
            except (TypeError, AttributeError):
                raise UsageError(
                    f"Error (input {idx + 1}): --align-day-of-year needs a calendar-date "
                    f"time axis, but '{tdim}' is not a date axis (e.g. a forecast "
                    f"'step' timedelta). Drop the flag or pick a date dim with "
                    f"--time-dim.",
                    prefix=False,
                ) from None
            if len(xvals) > 1 and np.any(np.diff(xvals) < 0):
                print(
                    f"Warning (input {idx + 1}): day-of-year values are non-monotonic "
                    f"(decrease at some point — a trace crossing a year boundary, "
                    f"spanning multiple years, or an out-of-order time axis); "
                    f"rendering anyway, but it may overplot itself on the shared "
                    f"day-of-year axis.",
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
    ylabel = variable if not units else f"{variable} [{units}]"
    ax.set_ylabel(ylabel)
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
