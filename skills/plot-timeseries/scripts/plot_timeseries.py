# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core",
#   "cf-xarray",
#   "cftime",
#   # matplotlib<3.10: keep the plot skills on one tested matplotlib
#   "matplotlib>=3.8,<3.10",
#   "nc-time-axis",
#   "numpy",
#   "xarray",
#   "zarr",
# ]
#
# [tool.uv.sources]
# weather-skills-core = { path = "../../../../weather-skills-core", editable = true }
# ///
"""Render a multi-input timeseries PNG from one or more weather-skills envelope Zarrs.

Each input contributes one 1D line trace on a shared set of axes, plotted
against its time-like coord. Inputs whose selected variable is not already
1D must list the dims to reduce via repeated --reduce flags; reductions are
mean-only and explicit (no silent averaging).
"""

import sys
from pathlib import Path

from weather_skills_core import Types, UsageError, weather_skill
from weather_skills_core.dataset import cf_dim

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


@weather_skill(
    name="plot-timeseries",
    version=_SKILL_VERSION,
    inputs=[Types.ANY + "+"],
    outputs=[Types.PNG],
    optional_args=("variable",),
)
@weather_skill.argument(
    "--reduce",
    action="append",
    default=None,
    help="Name of a non-time dim to mean-reduce before plotting. Repeatable.",
)
@weather_skill.argument(
    "--align-day-of-year",
    action="store_true",
    help=(
        "Plot each trace against day-of-year (1-366) instead of its absolute "
        "date, so inputs from different years overlay on a shared x-axis. "
        "Requires a calendar-date time axis (errors on a non-date axis such "
        "as a forecast 'step' timedelta)."
    ),
)
@weather_skill.argument("--title", default=None)
@weather_skill.argument(
    "--time-dim",
    default=None,
    help="Name of the time-like dim when not auto-detectable.",
)
def plot_timeseries(datasets, variable, time_dim, reduce, title, align_day_of_year):
    """Render a multi-input timeseries PNG from one or more weather-skills envelope Zarrs.

    Each input contributes one 1D line trace on a shared set of axes, plotted
    against its time-like coord. Inputs whose selected variable is not already
    1D must list the dims to reduce via repeated --reduce flags; reductions are
    mean-only and explicit (no silent averaging).
    """
    reduce = reduce or []
    paths = [Path(ds.encoding["source"]) if ds.encoding.get("source") else None for ds in datasets]

    import matplotlib

    matplotlib.use("Agg")
    import cf_xarray  # noqa: F401 — registers the .cf accessor
    import matplotlib.pyplot as plt
    import nc_time_axis  # noqa: F401 — registers the cftime→matplotlib axis converter
    import numpy as np

    if variable is not None and len(variable) != 1:
        raise UsageError(f"--variable must be given once; got {variable!r}")
    variable = variable[0] if variable else next(iter(datasets[0].data_vars), None)
    if variable is None:
        label0 = paths[0] if paths[0] is not None else "input 0"
        raise UsageError(f"no usable variable in {label0}.")
    for idx, ds in enumerate(datasets):
        if variable not in ds:
            label = paths[idx] if paths[idx] is not None else f"input {idx}"
            raise UsageError(
                f"variable '{variable}' missing from {label}. Available: {list(ds.data_vars)}"
            )

    # Input-units check. The traces share one y-axis whose label takes the
    # units of the first input. If the inputs hold the variable in different
    # units, traces measured in different units are drawn against a single
    # scale and labeled with only one of them, which misrepresents the data.
    # This only affects the rendering, so warn and proceed. Compare only the
    # inputs that carry the variable with a string `units` attr; an input that
    # lacks the variable is skipped, and a missing or non-string value can't be
    # checked. Units are compared after stripping surrounding whitespace so a
    # trailing space is not read as a real difference.
    seen_units = {}
    for idx, ds in enumerate(datasets):
        if variable not in ds:
            continue
        u = ds[variable].attrs.get("units")
        if isinstance(u, str):
            src = ds.encoding.get("source")
            seen_units[Path(src).stem if src else f"input{idx}"] = u.strip()
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
        src = ds.encoding.get("source")
        label = Path(src).stem if src else f"input{idx}"
        try:
            tdim = _pick_time_dim(da, time_dim)
        except ValueError as exc:
            raise UsageError(f"Error ({label}): {exc}", prefix=False) from None

        applicable = [d for d in reduce if d in da.dims]
        if applicable:
            da = da.mean(applicable, keep_attrs=True)

        extras = [d for d in da.dims if d != tdim]
        if extras:
            raise UsageError(
                f"Error ({label}): variable '{variable}' still has non-time dims "
                f"{extras} after --reduce. Pass --reduce <dim> for each.",
                prefix=False,
            )

        if align_day_of_year:
            # `.dt.dayofyear` works for datetime64 and object-dtype cftime time
            # coords; it raises TypeError/AttributeError on a non-calendar axis
            # (e.g. a forecast `step` timedelta), which we surface clearly.
            try:
                xvals = da[tdim].dt.dayofyear.values
            except (TypeError, AttributeError):
                raise UsageError(
                    f"Error ({label}): --align-day-of-year needs a calendar-date "
                    f"time axis, but '{tdim}' is not a date axis (e.g. a forecast "
                    f"'step' timedelta). Drop the flag or pick a date dim with "
                    f"--time-dim.",
                    prefix=False,
                ) from None
            # A non-monotonic day-of-year sequence draws over itself on the
            # shared axis. This happens when a trace crosses a year boundary,
            # spans multiple years, or has an out-of-order time axis. Rendering
            # caveat only — warn and proceed.
            if len(xvals) > 1 and np.any(np.diff(xvals) < 0):
                print(
                    f"Warning ({label}): day-of-year values are non-monotonic "
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
    return fig


if __name__ == "__main__":
    plot_timeseries()
