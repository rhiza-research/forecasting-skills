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
from weather_skills_core.standard_dataset import ALIASES, names_for
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


def _axis_label(text):
    """Sentence-case an axis label; map lon/lat shorthand to Longitude/Latitude."""
    if text is None:
        return text
    s = str(text).strip()
    if not s:
        return s
    known = {
        "lon": "Longitude",
        "lat": "Latitude",
        "longitude": "Longitude",
        "latitude": "Latitude",
        "valid time": "Valid time",
        "calendar day": "Calendar day",
        "time": "Time",
        "step": "Step",
        "forecast step": "Forecast step",
    }
    key = s.lower()
    if key in known:
        return known[key]
    if s[:1].islower():
        return s[:1].upper() + s[1:]
    return s


def _resolve_axis_label(override, default):
    """Use ``override`` verbatim when set; otherwise sentence-case ``default``."""
    if override is not None and str(override).strip() != "":
        return str(override)
    return _axis_label(default)


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
    "style": "style",
}
_LINE_ONLY_KEYS = frozenset({"linewidth", "linestyle", "marker", "markersize"})
_BAR_KEYS = frozenset({"color", "alpha", "zorder"})
_TRACE_STYLES = frozenset({"line", "bar"})
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
        if canon == "style":
            kind = val.casefold()
            if kind not in _TRACE_STYLES:
                raise ValueError(f"--trace style={val!r} must be line or bar")
            options[canon] = kind
        elif canon in {"linewidth", "markersize", "alpha", "zorder"}:
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
                f"--trace color={color!r} is not a matplotlib color (name, hex, or grayscale 0-1)."
            )


def _series_kind(style: dict, default: str, yvals=None) -> str:
    import numpy as np

    if yvals is not None and np.asarray(yvals).ndim == 2:
        if style.get("style") == "bar":
            raise UsageError(
                "--along traces are drawn as lines; do not set style=bar on an --along series."
            )
        return "line"
    kind = style.get("style", default)
    if kind not in _TRACE_STYLES:
        raise UsageError(f"--trace style={kind!r} must be line or bar")
    return kind


def _line_kwargs(style: dict, *, ensemble: bool = False) -> dict:
    defaults = (
        {"marker": "None", "linewidth": 0.8, "alpha": 0.4}
        if ensemble
        else {"marker": "o", "markersize": 5}
    )
    kw = {**defaults, **{k: v for k, v in style.items() if k != "style"}}
    marker = kw.get("marker")
    if isinstance(marker, str) and marker.casefold() in {"none", "null"}:
        kw["marker"] = "None"
        kw.pop("markersize", None)
    return kw


def _along_dim(da, along: str | None) -> str | None:
    """Resolve ``--along`` to a dim on ``da``, including ontology aliases (member/number)."""
    if not along:
        return None
    if along in da.dims:
        return along
    preferred = ALIASES.get(along, along)
    return next((name for name in names_for(preferred) if name in da.dims), None)


def _plot_line(ax, xvals, yvals, label, style):
    """Draw one series. 2D ``yvals`` (time × along) is one matplotlib call, one legend entry."""
    import numpy as np

    y = np.asarray(yvals)
    if y.ndim > 2:
        raise UsageError("timeseries y-values have more than 2 dims after reduce/--along")
    ensemble = y.ndim == 2
    kw = _line_kwargs(style, ensemble=ensemble)
    if not ensemble:
        ax.plot(xvals, y, label=label, **kw)
        return
    if "color" not in kw:
        kw["color"] = ax._get_lines.get_next_color()
    lines = ax.plot(xvals, y, **kw)
    if lines:
        lines[0].set_label(label)


def _bar_kwargs(style: dict) -> dict:
    extra = [k for k in style if k in _LINE_ONLY_KEYS]
    if extra:
        raise UsageError(
            f"--trace keys {sorted(extra)} apply to line traces, not bar traces. "
            "Use color, alpha, or zorder, or set style=line on this series."
        )
    return {k: v for k, v in style.items() if k in _BAR_KEYS}


def _draw_lines(ax, series, styles):
    for (xvals, yvals, label), style in zip(series, styles, strict=True):
        _plot_line(ax, xvals, yvals, label, style)


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


def _draw_mixed(ax, series, styles, kinds):
    """Bars grouped among bar traces only; lines at the un-offset x."""
    converted = []
    any_dates = False
    for (xvals, yvals, label), style, kind in zip(series, styles, kinds, strict=True):
        xnum, is_dates = _numeric_x(xvals)
        any_dates = any_dates or is_dates
        converted.append((kind, xnum, yvals, label, style))
    n_bars = sum(1 for kind, *_ in converted if kind == "bar")
    bar_xs = [xnum for kind, xnum, *_ in converted if kind == "bar"]
    group_span = 0.8 * min(_median_spacing(x) for x in bar_xs)
    bar_w = group_span / n_bars
    bar_i = 0
    for kind, xnum, yvals, label, style in converted:
        if kind == "bar":
            offset = (bar_i - (n_bars - 1) / 2) * bar_w
            ax.bar(
                xnum + offset,
                yvals,
                width=bar_w * 0.9,
                label=label,
                align="center",
                **_bar_kwargs(style),
            )
            bar_i += 1
        else:
            _plot_line(ax, xnum, yvals, label, style)
    if any_dates:
        ax.xaxis_date()


def _draw_traces(ax, series, styles, default_style):
    kinds = [
        _series_kind(style, default_style, yvals)
        for (_, yvals, _), style in zip(series, styles, strict=True)
    ]
    if all(kind == "bar" for kind in kinds):
        _draw_bars(ax, series, styles)
    elif all(kind == "line" for kind in kinds):
        _draw_lines(ax, series, styles)
    else:
        _draw_mixed(ax, series, styles, kinds)


def _legend_handles(ax, series):
    """Legend entries in ``--input`` order (mixed bar/line artists are not)."""
    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles, strict=True))
    ordered_labels = [label for _, _, label in series]
    return [by_label[label] for label in ordered_labels], ordered_labels


def _place_legend_below(ax, handles, labels, fontsize: int):
    """Place legend centered below the plotting area."""
    ncols = max(1, min(len(labels), 4))
    return ax.legend(
        handles,
        labels,
        fontsize=fontsize,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.14),
        ncol=ncols,
        frameon=False,
    )


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
@weather_skill.argument(
    "--along",
    default=None,
    help=(
        "Non-time dim to fan into traces (e.g. number/member). One input, "
        "one legend entry; not one --input per member."
    ),
)
@weather_skill.argument("--title", default=None, help="Optional figure title.")
@weather_skill.argument(
    "--xlabel",
    default=None,
    help="Override the x-axis label (default: Time / Valid time / Calendar day).",
)
@weather_skill.argument(
    "--ylabel",
    default=None,
    help="Override the y-axis label (default: from variable metadata).",
)
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
        "marker, markersize, alpha, zorder, style (line|bar, overrides "
        "global --style for that series)."
    ),
)
def plot_timeseries(
    ds,
    variable,
    time_dim,
    reduce,
    along,
    title,
    xlabel,
    ylabel,
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
        along_dim = _along_dim(da, along)
        if along_dim == tdim:
            raise UsageError(
                f"Error (input {idx + 1}): --along {along!r} is the time axis "
                f"('{tdim}'); pass a non-time dim such as number.",
                prefix=False,
            )
        if along and along_dim is None and extras:
            raise UsageError(
                f"Error (input {idx + 1}): --along {along!r} is not a dim of "
                f"variable '{variable}' (dims: {list(da.dims)}).",
                prefix=False,
            )
        if along_dim:
            extras = [d for d in extras if d != along_dim]
        if extras:
            hint = extras[0]
            raise UsageError(
                f"Error (input {idx + 1}): variable '{variable}' still has non-time dims "
                f"{extras} after --reduce. Pass --reduce <dim> for each, or "
                f"--along {hint} to draw one line per {hint} value.",
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
        if along_dim:
            da = da.transpose(tdim, along_dim)
        series.append((xvals, np.asarray(da.values), label))

        if first_tdim is None:
            first_tdim = tdim
        if axis_label is None:
            axis_label = xlabel

    styles = resolve_trace_styles([label for _, _, label in series], trace)
    _validate_trace_colors(styles)
    _draw_traces(ax, series, styles, style)

    tick_fs = max(10, int(round(fontsize * 0.7)))
    legend_fs = max(10, int(round(fontsize * 0.85)))
    ax.set_xlabel(
        _resolve_axis_label(xlabel, axis_label or first_tdim or "time"), fontsize=fontsize
    )
    ax.set_ylabel(
        _resolve_axis_label(ylabel, _y_label(variable, datasets[0][variable])),
        fontsize=fontsize,
    )
    if title:
        ax.set_title(title, fontsize=fontsize)
    ax.tick_params(labelsize=tick_fs)
    handles, legend_labels = _legend_handles(ax, series)
    _place_legend_below(ax, handles, legend_labels, legend_fs)
    ax.grid(True, linestyle="--", alpha=0.5)

    if align_day_of_year:
        _apply_day_of_year_ticks(ax)
    else:
        fig.autofmt_xdate()
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_timeseries()
