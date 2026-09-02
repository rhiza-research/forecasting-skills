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

import argparse
import re
import sys
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.cf import auto_variable
from weather_skills_core.display_labels import dataset_display_label, resolve_input_labels
from weather_skills_core.standard_utils import pick_time_dim
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


_TRACE_KEYS = {
    "color": "color",
    "linewidth": "linewidth",
    "lw": "linewidth",
    "linestyle": "linestyle",
    "ls": "linestyle",
    "marker": "marker",
    "markersize": "markersize",
    "ms": "markersize",
    "alpha": "alpha",
    "zorder": "zorder",
}
_LINE_ONLY_KEYS = frozenset({"linewidth", "linestyle", "marker", "markersize"})
_BAR_KEYS = frozenset({"color", "alpha", "zorder"})
_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z]+")


class TraceSpec:
    """One ``--trace SELECTOR:k=v[,k=v...]`` entry."""

    def __init__(self, selector, options, raw):
        self.selector = selector
        self.options = options
        self.raw = raw

    def __str__(self):
        return self.raw

    def __repr__(self):
        return f"TraceSpec({self.raw!r})"


def _parse_trace_options(blob: str) -> dict:
    """Parse ``k=v,k=v`` into canonical matplotlib kwargs."""
    if not blob.strip():
        raise ValueError("--trace needs at least one k=v option (e.g. color=black)")
    options = {}
    for token in blob.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(
                f"--trace option {token!r} is not k=v; expected color=, linewidth=, ..."
            )
        key, _, val = token.partition("=")
        key, val = key.strip(), val.strip()
        if not key:
            raise ValueError(f"--trace option {token!r} has an empty key")
        canon = _TRACE_KEYS.get(key)
        if canon is None:
            raise ValueError(
                f"unknown --trace option {key!r}; expected one of "
                f"{', '.join(sorted(set(_TRACE_KEYS.values())))}"
            )
        if not val:
            raise ValueError(f"--trace option {key!r} has an empty value")
        if canon in options:
            raise ValueError(f"--trace option {key!r} is given more than once")
        if canon in {"linewidth", "markersize", "alpha", "zorder"}:
            try:
                num = float(val)
            except ValueError as exc:
                raise ValueError(f"--trace {key}={val!r} is not a number") from exc
            if canon == "alpha" and not 0.0 <= num <= 1.0:
                raise ValueError(f"--trace alpha={val!r} must be between 0 and 1")
            options[canon] = num
        else:
            options[canon] = val
    if not options:
        raise ValueError("--trace needs at least one k=v option (e.g. color=black)")
    return options


def parse_trace(value) -> TraceSpec:
    """Argparse converter for ``SELECTOR:k=v[,k=v...]``."""
    if not value or not str(value).strip():
        raise argparse.ArgumentTypeError("--trace spec is empty")
    raw = str(value).strip()
    if ":" not in raw:
        raise argparse.ArgumentTypeError(
            f"--trace {raw!r} must be SELECTOR:k=v (e.g. 2026:color=black,linewidth=2.5)"
        )
    selector, _, blob = raw.partition(":")
    selector = selector.strip()
    if not selector:
        raise argparse.ArgumentTypeError(f"--trace {raw!r} is missing a selector")
    try:
        options = _parse_trace_options(blob)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return TraceSpec(selector, options, raw)


def _label_tokens(label: str) -> set[str]:
    return {part.casefold() for part in _TOKEN_SPLIT.split(label) if part}


def _trace_match_indices(selector: str, labels: list[str]) -> list[int]:
    """1-based index, exact legend label, or a unique alphanumeric token in the label."""
    if selector == "*":
        return list(range(len(labels)))
    if selector.isdigit():
        idx = int(selector) - 1
        if 0 <= idx < len(labels):
            return [idx]
    folded = selector.casefold()
    exact = [i for i, label in enumerate(labels) if label.casefold() == folded]
    if exact:
        return exact
    return [i for i, label in enumerate(labels) if folded in _label_tokens(label)]


def resolve_trace_styles(labels: list[str], specs: list[TraceSpec] | None) -> list[dict]:
    """Merge ``--trace`` specs onto one style dict per series (``*`` first, then specific)."""
    styles = [{} for _ in labels]
    if not specs:
        return styles
    wildcards = [spec for spec in specs if spec.selector == "*"]
    specific = [spec for spec in specs if spec.selector != "*"]
    for spec in wildcards:
        for style in styles:
            style.update(spec.options)
    for spec in specific:
        hits = _trace_match_indices(spec.selector, labels)
        if not hits:
            raise UsageError(
                f"--trace {spec.raw!r} matched no series. Selectors are a 1-based --input "
                f"index, a legend label, a unique token in a label (e.g. 2026), or *. "
                f"Legend labels: {labels}."
            )
        if len(hits) > 1:
            matched = [labels[i] for i in hits]
            raise UsageError(
                f"--trace {spec.raw!r} matched more than one series ({matched}). "
                "Use a 1-based --input index or a more specific label."
            )
        styles[hits[0]].update(spec.options)
    return styles


def _validate_trace_colors(styles: list[dict]) -> None:
    import matplotlib.colors as mcolors

    for style in styles:
        color = style.get("color")
        if color is not None and not mcolors.is_color_like(color):
            raise UsageError(
                f"--trace color={color!r} is not a matplotlib color "
                "(name, hex, or grayscale 0-1)."
            )


def _line_kwargs(style: dict) -> dict:
    kw = {"marker": "o", "markersize": 5, **style}
    marker = kw.get("marker")
    if isinstance(marker, str) and marker.casefold() in {"none", "null"}:
        kw["marker"] = "None"
        kw.pop("markersize", None)
    return kw


def _bar_kwargs(style: dict) -> dict:
    extra = [k for k in style if k in _LINE_ONLY_KEYS]
    if extra:
        raise UsageError(
            f"--trace keys {sorted(extra)} apply to --style line, not --style bar. "
            "Use color, alpha, or zorder."
        )
    return {k: v for k, v in style.items() if k in _BAR_KEYS}


def _draw_lines(ax, series, styles):
    for (xvals, yvals, label), style in zip(series, styles, strict=True):
        ax.plot(xvals, yvals, label=label, **_line_kwargs(style))


def _trace_label(ds, idx: int, override: str | None = None) -> str:
    """Legend label: explicit --label, station id, filename stem, else provenance."""
    if override:
        return override
    station = _size1_str(ds, "station_id", "point_id")
    if station:
        name = _size1_str(ds, "name")
        if name and name.casefold() != station.casefold():
            return f"{station} {name}"
        return station
    stem = _source_stem(ds)
    if stem:
        return stem
    return dataset_display_label(ds, f"input {idx + 1}")


def _y_label(variable, da):
    return variable_label_for_display(da, fallback=variable)


def _day_of_year_tick_label(doy: float) -> str:
    """Map a 1-based day-of-year tick value to a short calendar label."""
    import datetime as dt

    day = int(round(doy))
    if day < 1 or day > 366:
        return ""
    if day == 366:
        return "Dec 31"
    date = dt.date(2023, 1, 1) + dt.timedelta(days=day - 1)
    return f"{date.strftime('%b')} {date.day}"


def _month_start_day_of_year_ticks(xmin: float, xmax: float) -> tuple[list[float], list[str]]:
    """First-of-month tick positions and labels within ``[xmin, xmax]``."""
    import datetime as dt

    positions: list[float] = []
    labels: list[str] = []
    for month in range(1, 13):
        date = dt.date(2023, month, 1)
        doy = float(date.timetuple().tm_yday)
        if xmin <= doy <= xmax:
            positions.append(doy)
            labels.append(f"{date.strftime('%b')} {date.day}")
    return positions, labels


def _apply_day_of_year_ticks(ax) -> None:
    """Label day-of-year x ticks with calendar dates (e.g. Oct 1, not 274)."""
    from matplotlib.ticker import FixedFormatter, FixedLocator, FuncFormatter, MaxNLocator

    xmin, xmax = ax.get_xlim()
    positions, labels = _month_start_day_of_year_ticks(xmin, xmax)
    if len(positions) >= 2:
        ax.xaxis.set_major_locator(FixedLocator(positions))
        ax.xaxis.set_major_formatter(FixedFormatter(labels))
        return

    ax.xaxis.set_major_locator(MaxNLocator(nbins=8, integer=True, min_n_ticks=3))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _pos: _day_of_year_tick_label(x)))


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


def _draw_bars(ax, series, styles):
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
        ax.bar(
            xnum + offset,
            yvals,
            width=bar_w * 0.9,
            label=label,
            align="center",
            **_bar_kwargs(styles[i]),
        )
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
    "--fontsize",
    type=int,
    default=16,
    help="Base font size for titles, axis labels, ticks, and legend (default 16).",
)
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
@weather_skill.argument(
    "--label",
    action="append",
    default=None,
    help="Legend label for each --input, in order. Omit to infer from metadata.",
)
@weather_skill.argument(
    "--trace",
    action="append",
    default=[],
    type=parse_trace,
    help=(
        "Per-series style SELECTOR:k=v. Repeatable. Selector is a 1-based "
        "--input index (1..N), legend label, unique token in the label "
        "(e.g. 2026), or * for all. Keys: color, linewidth, linestyle, "
        "marker, markersize, alpha, zorder."
    ),
)
def plot_timeseries(
    ds,
    variable,
    time_dim,
    reduce,
    title,
    fontsize,
    style,
    align_day_of_year,
    label,
    trace,
    output,
    **kwargs,
):
    """Render a multi-input timeseries PNG from weather-skills standard dataset Zarrs."""
    if not isinstance(ds, (list, tuple)):
        datasets = [ds]
    else:
        datasets = list(ds)
    if len(datasets) > 26:
        raise UsageError(f"--input must be passed at most 26 times; got {len(datasets)}.")
    label_slots = resolve_input_labels(label, len(datasets))

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
            seen_units[_trace_label(ds, idx, label_slots[idx])] = u.strip()
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

        label = _trace_label(ds, idx, label_slots[idx])
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
            xlabel = "calendar day"
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

    styles = resolve_trace_styles([label for _, _, label in series], trace)
    _validate_trace_colors(styles)
    if style == "bar":
        _draw_bars(ax, series, styles)
    else:
        _draw_lines(ax, series, styles)

    tick_fs = max(10, int(round(fontsize * 0.7)))
    legend_fs = max(10, int(round(fontsize * 0.85)))
    ax.set_xlabel(axis_label or first_tdim or "time", fontsize=fontsize)
    ax.set_ylabel(_y_label(variable, datasets[0][variable]), fontsize=fontsize)
    if title:
        ax.set_title(title, fontsize=fontsize)
    ax.tick_params(labelsize=tick_fs)
    ax.legend(fontsize=legend_fs)
    ax.grid(True, linestyle="--", alpha=0.5)

    if align_day_of_year:
        _apply_day_of_year_ticks(ax)
    else:
        fig.autofmt_xdate()
    fig.tight_layout()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_timeseries()
