# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
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
    precip_for_display,
    to_standard_units,
    units_equal,
    variable_label_for_display,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _size1_str(ds, *names) -> str | None:
    for name in names:
        if name not in ds.coords and name not in getattr(ds, "variables", ()):
            continue
        arr = ds[name]
        if arr.size != 1:
            continue
        text = str(arr.values.reshape(-1)[0]).strip()
        if text:
            return text
    return None


def _source_stem(ds) -> str | None:
    source = ds.encoding.get("source")
    if isinstance(source, str) and source.strip():
        stem = Path(source).stem
        if stem:
            return stem
    return None


def _trace_label(ds, idx: int) -> str:
    """Legend label: station id, else filename stem, else weather_skills_source."""
    station = _size1_str(ds, "station_id", "point_id")
    if station:
        name = _size1_str(ds, "name")
        if name and name.casefold() != station.casefold():
            return f"{station} {name}"
        return station
    stem = _source_stem(ds)
    if stem:
        return stem
    return dataset_label(ds, f"input {idx + 1}")


def _y_label(variable, da):
    return variable_label_for_display(da, fallback=variable)


def _numeric_x(xvals):
    """Map plot x values to a numeric axis; True when they were calendar dates."""
    import matplotlib.dates as mdates
    import numpy as np

    x = np.asarray(xvals)
    if x.dtype.kind == "M":
        return mdates.date2num(x), True
    if x.dtype == object:
        first = next((v for v in x.flat if v is not None), None)
        if first is not None and hasattr(first, "timetuple"):
            return np.asarray(mdates.date2num(x), dtype=float), True
    return np.asarray(x, dtype=float), False


def _median_spacing(xnum):
    import numpy as np

    x = np.sort(np.unique(xnum))
    if x.size < 2:
        return 1.0
    return float(np.median(np.diff(x)))


def _draw_bars(ax, series):
    """Grouped bars on a shared numeric x (dates are converted and restored)."""
    converted = []
    any_dates = False
    for xvals, yvals, label in series:
        xnum, is_dates = _numeric_x(xvals)
        any_dates = any_dates or is_dates
        converted.append((xnum, yvals, label))
    n = len(converted)
    group_span = 0.8 * min(_median_spacing(x) for x, _, _ in converted)
    bar_w = group_span / n
    for i, (xnum, yvals, label) in enumerate(converted):
        offset = (i - (n - 1) / 2) * bar_w
        ax.bar(xnum + offset, yvals, width=bar_w * 0.9, label=label, align="center")
    if any_dates:
        ax.xaxis_date()


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
    "--style",
    choices=["line", "bar"],
    default="line",
    help="line (default) or grouped bar.",
)
@weather_skill.argument(
    "--align-day-of-year",
    action="store_true",
    help="Plot against day-of-year (1-366) instead of absolute date.",
)
def plot_timeseries(
    ds, variable, time_dim, reduce, title, style, align_day_of_year, output, **kwargs
):
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
            seen_units[_trace_label(ds, idx)] = u.strip()
    if unit_vals and any(not units_equal(unit_vals[0], u) for u in unit_vals[1:]):
        detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
        print(
            f"Warning: variable '{variable}' has differing units across the "
            f"overlaid inputs ({detail}). The series share one y-axis labeled "
            f"with a single unit, so values in different units are not directly "
            f"comparable in this figure.",
            file=sys.stderr,
        )

    fig, ax = plt.subplots(figsize=(10, 6))
    first_tdim = None
    axis_label = None
    series = []

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

        label = _trace_label(ds, idx)
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
        series.append((xvals, da.values, label))

        if first_tdim is None:
            first_tdim = tdim
        if axis_label is None:
            axis_label = xlabel

    if style == "bar":
        _draw_bars(ax, series)
    else:
        for xvals, yvals, label in series:
            ax.plot(xvals, yvals, label=label, marker="o", markersize=5)

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
