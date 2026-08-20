# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "cf-xarray",
#   "cftime",
#   "xarray",
#   "numpy",
#   "pandas",
#   "pint-xarray>=0.6",
# ]
# ///
"""Temporal aggregation: calendar resample, rolling window, or step buckets (rates)."""

import datetime as _dt
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import roll_and_agg
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    DATA_INTERVAL_ATTR,
    PERIOD_TO_AGGREGATION,
    data_interval_of,
    expected_samples_in_period,
    format_cell_methods,
    format_duration,
    infer_timestep,
    parse_aggregation_period,
    stamp_data_interval,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10}
RESAMPLE_FREQ = {"daily": "1D", "weekly": "7D", "dekadal": "10D", "monthly": "MS"}
ANCHOR_PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10, "monthly": 30}
_METHOD_TO_CF = {"mean": "mean", "max": "maximum", "min": "minimum"}
_NAMED_PERIODS = tuple(PERIOD_TO_AGGREGATION)


def _resolve_period(period: str) -> dict:
    """Named period or a whole-day pint duration (e.g. ``21 day``)."""
    if period in PERIOD_TO_AGGREGATION:
        return {
            "key": period,
            "agg": PERIOD_TO_AGGREGATION[period],
            "freq": RESAMPLE_FREQ[period],
            "anchor_days": ANCHOR_PERIOD_DAYS[period],
        }
    try:
        quantity = parse_aggregation_period(period)
    except UsageError as exc:
        raise UsageError(
            f"--period {period!r} must be one of {', '.join(_NAMED_PERIODS)} "
            f"or a pint duration in whole days (e.g. '21 day'): {exc}"
        ) from None
    days = float(quantity.to("day").magnitude)
    if days <= 0 or abs(days - round(days)) > 1e-6:
        raise UsageError(
            f"--period {period!r} must be a named period "
            f"({', '.join(_NAMED_PERIODS)}) or a whole number of days "
            "(e.g. '21 day')"
        )
    ndays = int(round(days))
    return {
        "key": period,
        "agg": format_duration(quantity),
        "freq": f"{ndays}D",
        "anchor_days": ndays,
    }


def _native_interval(ds, dim) -> str | None:
    stamped = data_interval_of(ds)
    if stamped:
        return stamped
    try:
        return format_duration(infer_timestep(ds, dim))
    except UsageError:
        return None


def _coverage_cell(ds) -> str | None:
    """Current cell width for coverage: re-agg uses aggregation_period, else native."""
    for name in ds.data_vars:
        val = ds[name].attrs.get(AGGREGATION_PERIOD_ATTR)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return data_interval_of(ds)


def _cf_bounds(ds, dim):
    """``(N, 2)`` start/end array if CF bounds exist, else None."""
    import numpy as np

    name = ds[dim].attrs.get("bounds") if dim in ds.coords or dim in ds.dims else None
    if not (isinstance(name, str) and name in ds):
        name = f"{dim}_bounds" if f"{dim}_bounds" in ds else None
    if name is None:
        return None
    arr = np.asarray(ds[name].values)
    if arr.ndim != 2 or arr.shape[-1] != 2:
        raise UsageError(f"{name!r} must have shape ({dim}, 2) start/end")
    return arr.reshape(arr.shape[0], 2)


def _as_ns(val):
    import numpy as np

    arr = np.asarray(val)
    if arr.dtype.kind == "m":
        return arr.astype("timedelta64[ns]").astype(np.int64)
    if arr.dtype.kind == "M":
        return arr.astype("datetime64[ns]").astype(np.int64)
    raise UsageError(f"cannot convert {arr.dtype} to a duration for CF bounds")


def _drop_bounds(ds, dim):
    name = ds[dim].attrs.get("bounds") if dim in ds.coords or dim in ds.dims else None
    names = []
    if isinstance(name, str) and name in ds:
        names.append(name)
    extra = f"{dim}_bounds"
    if extra in ds and extra not in names:
        names.append(extra)
    if names:
        ds = ds.drop_vars(names)
    if dim in ds.coords:
        attrs = dict(ds[dim].attrs)
        attrs.pop("bounds", None)
        ds[dim].attrs = attrs
    return ds


def _weighted_mean(ds, dim, indices, weights):
    import numpy as np
    import xarray as xr

    w = np.asarray(weights, dtype=float)
    sub = _drop_bounds(ds.isel({dim: indices}), dim)
    for name in sub.data_vars:
        da = sub[name]
        if getattr(da, "pint", None) is not None and da.pint.units is not None:
            sub[name] = da.pint.dequantify()
    wda = xr.DataArray(w, dims=[dim], coords={dim: sub[dim].values})
    return (sub * wda).sum(dim=dim, skipna=True, keep_attrs=True) / wda.sum()


def _empty_bins_message(spec, dim):
    return f"no {spec['key']} bins contained samples on {dim}"


def _aggregate_from_bounds(ds, dim, spec, method, bins):
    """Duration-weight samples into ``bins`` of ``(left, right, label)``."""
    import numpy as np
    import xarray as xr

    bounds = _cf_bounds(ds, dim)
    starts = _as_ns(bounds[:, 0])
    ends = _as_ns(bounds[:, 1])
    work = _drop_bounds(ds, dim)
    chunks, labels, coverages = [], [], []
    for left, right, label in bins:
        left_ns = int(np.asarray(_as_ns(left)).reshape(-1)[0])
        right_ns = int(np.asarray(_as_ns(right)).reshape(-1)[0])
        period_ns = right_ns - left_ns
        if period_ns <= 0:
            continue
        overlap = np.minimum(ends, right_ns) - np.maximum(starts, left_ns)
        overlap = np.maximum(overlap, 0)
        idx = np.flatnonzero(overlap > 0)
        if idx.size == 0:
            continue
        covered = float(overlap[idx].sum())
        coverage = min(1.0, covered / period_ns)
        if method == "mean":
            chunks.append(_weighted_mean(work, dim, idx, overlap[idx]))
        else:
            chunks.append(_reduce(work.isel({dim: idx}), method=method, dim=dim))
        labels.append(label)
        coverages.append(coverage)
    if not chunks:
        raise UsageError(_empty_bins_message(spec, dim))
    out = xr.concat(chunks, dim=dim).assign_coords({dim: labels})
    return _assign_coverage(out, dim, coverages)


def _coverage_fraction(n_present: int, expected: int) -> float:
    if expected <= 0:
        return 1.0
    return min(1.0, n_present / expected)


def _assign_coverage(out, dim, coverages):
    import numpy as np

    if out.sizes.get(dim, 0) == 0:
        return out
    values = np.asarray(coverages, dtype=float)
    out = out.assign_coords({AGGREGATION_COVERAGE_COORD: (dim, values)})
    out[AGGREGATION_COVERAGE_COORD].attrs.update(
        long_name="fraction of native samples present in the interval",
        units="1",
        valid_min=0.0,
        valid_max=1.0,
    )
    return out


def _reduce(grouped, method, dim=None):
    fn = {"mean": grouped.mean, "max": grouped.max, "min": grouped.min}[method]
    return fn(dim=dim, keep_attrs=True) if dim is not None else fn(keep_attrs=True)


def _group_indices(group, size: int):
    import numpy as np

    if isinstance(group, slice):
        return list(range(*group.indices(size)))
    return list(np.asarray(group).ravel())


def _timestep_days(ds, dim) -> float:
    try:
        return float(infer_timestep(ds, dim).to("day").magnitude)
    except UsageError:
        return 1.0


def _expected_samples(
    period: str, label, timestep_days: float, *, period_days: int | None = None
) -> int:
    """How many input samples a complete bin of ``period`` should hold."""
    if period_days is not None:
        days = period_days
    elif period == "monthly":
        import pandas as pd

        days = int(pd.Timestamp(label).days_in_month)
    else:
        days = PERIOD_DAYS[period]
    return max(1, int(round(days / timestep_days)))


def _expected_from_interval(spec, label, native_interval, timestep_days: float) -> int:
    if native_interval:
        try:
            if spec["key"] == "monthly":
                import pandas as pd

                days = int(pd.Timestamp(label).days_in_month)
                period = f"{days} day"
            else:
                period = spec["agg"]
            return expected_samples_in_period(period, native_interval)
        except UsageError:
            pass
    period_days = None if spec["key"] == "monthly" else spec["anchor_days"]
    return _expected_samples(spec["key"], label, timestep_days, period_days=period_days)


def _aggregate_time_resample(ds, dim, spec, method):
    """Forward calendar resample; stamp ``aggregation_coverage`` on every bin with samples."""
    import numpy as np
    import xarray as xr

    freq = spec["freq"]
    if _cf_bounds(ds, dim) is not None:
        resampler = ds.resample({dim: freq})
        bins = []
        for label in resampler.groups:
            if spec["key"] == "monthly":
                import pandas as pd

                left, right = label, pd.Timestamp(label) + pd.offsets.MonthBegin(1)
            elif hasattr(label, "calendar"):
                left = label
                right = label + _dt.timedelta(days=spec["anchor_days"])
            else:
                left = np.datetime64(label)
                right = left + np.timedelta64(spec["anchor_days"], "D")
            bins.append((left, right, left if hasattr(label, "calendar") else np.datetime64(label)))
        return _aggregate_from_bounds(ds, dim, spec, method, bins)

    timestep_days = _timestep_days(ds, dim)
    native_interval = _coverage_cell(ds)
    resampler = ds.resample({dim: freq})
    n = ds.sizes[dim]
    chunks, labels, coverages = [], [], []
    for label, group in resampler.groups.items():
        indices = _group_indices(group, n)
        if not indices:
            continue
        expected = _expected_from_interval(spec, label, native_interval, timestep_days)
        chunks.append(_reduce(ds.isel({dim: indices}), method=method, dim=dim))
        labels.append(np.datetime64(label) if not hasattr(label, "calendar") else label)
        coverages.append(_coverage_fraction(len(indices), expected))
    if not chunks:
        raise UsageError(_empty_bins_message(spec, dim))
    out = xr.concat(chunks, dim=dim).assign_coords({dim: labels})
    return _assign_coverage(out, dim, coverages)


def _aggregate_time_anchored(ds, dim, spec, method, end_time, *, start_time=None):
    """Backward fixed-width bins ending at ``end_time``.

    Walks while the bin's right edge is still after the effective data start,
    then keeps bins that contain samples and stamps ``aggregation_coverage``.
    Optional ``start_time`` raises the earliest coverage floor.
    """
    import numpy as np
    import xarray as xr

    period_days = spec["anchor_days"]
    raw = np.asarray(ds[dim].values)
    if raw.size == 0:
        raise UsageError(_empty_bins_message(spec, dim))
    if raw.dtype.kind == "O" and hasattr(raw.flat[0], "calendar"):
        import cftime

        calendar = raw.flat[0].calendar
        bin_width = _dt.timedelta(days=period_days)
        data_min = raw.min()
        if start_time is not None:
            try:
                start_cf = cftime.datetime(
                    start_time.year, start_time.month, start_time.day, calendar=calendar
                )
            except ValueError:
                raise UsageError(
                    f"--start-time {start_time.isoformat()} invalid in {calendar!r}"
                ) from None
            if start_cf > data_min:
                data_min = start_cf
        try:
            right = cftime.datetime(end_time.year, end_time.month, end_time.day, calendar=calendar)
        except ValueError:
            raise UsageError(f"--end-time {end_time.isoformat()} invalid in {calendar!r}") from None
        label = lambda edge: edge  # noqa: E731
    else:
        import pandas as pd

        bin_width = pd.Timedelta(days=period_days)
        data_min = pd.Timestamp(pd.to_datetime(raw).min())
        if start_time is not None:
            start_ts = pd.Timestamp(start_time)
            if start_ts > data_min:
                data_min = start_ts
        right = pd.Timestamp(end_time)
        label = lambda edge: np.datetime64(edge)  # noqa: E731

    timestep_days = _timestep_days(ds, dim)
    native_interval = _coverage_cell(ds)
    bins, r = [], right
    # Stop once the right edge is at/before the first sample — do not require
    # left >= data_min, or a bin that still holds data is never created.
    while r > data_min:
        left = r - bin_width
        bins.append((left, r))
        r = left
    bins.reverse()
    if _cf_bounds(ds, dim) is not None:
        bound_bins = [(left, right, label(right)) for left, right in bins]
        return _aggregate_from_bounds(ds, dim, spec, method, bound_bins)
    chunks, labels, coverages = [], [], []
    for left, right in bins:
        keep = np.nonzero((raw > left) & (raw <= right))[0]
        if keep.size == 0:
            continue
        expected = _expected_from_interval(spec, right, native_interval, timestep_days)
        chunks.append(_reduce(ds.isel({dim: keep}), method=method, dim=dim))
        labels.append(label(right))
        coverages.append(_coverage_fraction(keep.size, expected))
    if not chunks:
        raise UsageError(_empty_bins_message(spec, dim))
    out = xr.concat(chunks, dim=dim).assign_coords({dim: labels})
    return _assign_coverage(out, dim, coverages)


def _aggregate_step(ds, spec, method):
    import numpy as np
    import pandas as pd
    import xarray as xr

    if spec["key"] == "monthly":
        raise UsageError("monthly aggregation is not defined for step")
    window = pd.Timedelta(days=spec["anchor_days"]).to_timedelta64()
    steps = ds["step"].values
    bounds = _cf_bounds(ds, "step")
    max_step = bounds[:, 1].max() if bounds is not None else steps.max()
    edges = np.arange(0, max_step + window, window, dtype=steps.dtype)
    if bounds is not None:
        bins = []
        for left in edges[:-1]:
            right = left + window
            bins.append((left, right, left + window))
        return _aggregate_from_bounds(ds, "step", spec, method, bins)
    native_interval = _coverage_cell(ds)
    timestep_days = _timestep_days(ds, "step")
    chunks, labels, coverages = [], [], []
    for left in edges[:-1]:
        right = left + window
        mask = (steps > left) & (steps <= right)
        if not mask.any():
            continue
        n_present = int(mask.sum())
        expected = _expected_from_interval(spec, right, native_interval, timestep_days)
        chunks.append(_reduce(ds.isel(step=np.where(mask)[0]), method=method, dim="step"))
        labels.append(left + window)
        coverages.append(_coverage_fraction(n_present, expected))
    if not chunks:
        raise UsageError(_empty_bins_message(spec, "step"))
    out = xr.concat(chunks, dim="step").assign_coords(step=labels)
    return _assign_coverage(out, "step", coverages)


def _stamp_attrs(out, dim, agg_period, method, interval, *, data_interval=None):
    cf_method = _METHOD_TO_CF[method]
    cm = format_cell_methods(dim, cf_method, interval=interval)
    for name in out.data_vars:
        attrs = {**out[name].attrs, AGGREGATION_PERIOD_ATTR: agg_period, "cell_methods": cm}
        native = attrs.get(DATA_INTERVAL_ATTR) or data_interval
        if native:
            attrs[DATA_INTERVAL_ATTR] = native
        out[name].attrs = attrs
    return out


def _rolling_aggregation_period(ds, dim, window, interval):
    """Pint duration for a rolling window of ``window`` input steps."""
    if interval is not None:
        try:
            from weather_skills_core.units import parse_aggregation_period

            step = parse_aggregation_period(interval)
            return format_duration(step * window)
        except UsageError:
            pass
    return f"{window} day"


@weather_skill(
    name="aggregate-temporal",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "-i",
    "--input",
    type=Dataset(["time", "prediction_timedelta"]),
    required=True,
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--period",
    default=None,
    help=(
        "Calendar/step window: daily, weekly, dekadal, monthly, or a pint "
        "duration in whole days (e.g. '21 day'). Mutex with --window."
    ),
)
@weather_skill.argument(
    "--window",
    type=int,
    default=None,
    help="Rolling window in axis steps (mutex with --period).",
)
@weather_skill.argument("--align", default="left", choices=["left", "right", "center"])
@weather_skill.argument(
    "--stride",
    default=None,
    help="With --window: int step or stride_dates string (day/week/Monday/...).",
)
@weather_skill.argument("--method", default="mean", choices=["mean", "max", "min"])
@weather_skill.argument("--time-dim", default=None)
@weather_skill.argument(
    "--end-time",
    default=None,
    help="With --period: date the final bin ends on (bins walk backward).",
)
@weather_skill.argument(
    "--start-time",
    default=None,
    help="With --period and --end-time: optional earliest coverage floor.",
)
def aggregate(
    ds,
    output,
    variable,
    time_dim,
    period,
    window,
    align,
    stride,
    method,
    end_time,
    start_time,
    **kwargs,
):
    """Temporal aggregation of rates: calendar resample, rolling, or step buckets."""
    import cf_xarray  # noqa: F401

    if (period is None) == (window is None):
        raise UsageError("exactly one of --period or --window is required")
    if window is not None and (end_time is not None or start_time is not None):
        raise UsageError("--start-time/--end-time cannot be combined with --window")
    if start_time is not None and end_time is None:
        raise UsageError("--start-time requires --end-time")
    if window is None and stride is not None:
        raise UsageError("--stride requires --window")
    if window is None and align != "left":
        raise UsageError("--align requires --window")

    if variable is not None:
        ds = ds[list(dict.fromkeys(variable))]

    if time_dim:
        dim = time_dim
    else:
        try:
            cf_time = ds.cf["time"].name
        except KeyError:
            cf_time = "time" if "time" in ds.dims else None
        if cf_time is not None and cf_time in ds.dims:
            dim = "step" if ds.sizes[cf_time] == 1 and "step" in ds.dims else cf_time
        elif "step" in ds.dims:
            dim = "step"
        else:
            raise UsageError(f"no time/step dim in {list(ds.dims)}; pass --time-dim")

    has_bounds = _cf_bounds(ds, dim) is not None
    if not has_bounds:
        try:
            ds = stamp_data_interval(ds, dim=dim)
        except UsageError:
            pass
        has_bounds = _cf_bounds(ds, dim) is not None
    native_interval = None if has_bounds else _native_interval(ds, dim)
    interval = native_interval
    if not has_bounds:
        try:
            if interval is None:
                interval = format_duration(infer_timestep(ds, dim))
        except UsageError:
            pass

    stride_val = stride
    if stride is not None:
        try:
            stride_val = int(stride)
        except ValueError:
            pass

    if window is not None:
        if has_bounds:
            raise UsageError(
                "--window is a step count; irregular axes with CF bounds require --period"
            )
        out = roll_and_agg(ds, window, dim, method, align=align, stride=stride_val)
        n = out.sizes.get(dim, 0)
        if n:
            out = _assign_coverage(out, dim, [1.0] * n)
        agg_period = _rolling_aggregation_period(ds, dim, window, interval)
        return _stamp_attrs(out, dim, agg_period, method, interval, data_interval=native_interval)

    spec = _resolve_period(period)
    if dim == "step":
        out = _aggregate_step(ds, spec, method)
        if end_time is not None or start_time is not None:
            print(
                "--start-time/--end-time have no effect on step aggregation",
                file=sys.stderr,
            )
    elif end_time is not None:
        out = _aggregate_time_anchored(
            ds,
            dim,
            spec,
            method,
            end_time,
            start_time=start_time,
        )
    else:
        out = _aggregate_time_resample(ds, dim, spec, method)

    return _stamp_attrs(out, dim, spec["agg"], method, interval, data_interval=native_interval)


if __name__ == "__main__":
    aggregate()
