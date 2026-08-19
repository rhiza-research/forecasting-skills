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
from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import roll_and_agg
from weather_skills_core.units import (
    AGGREGATION_PERIOD_ATTR,
    PERIOD_TO_AGGREGATION,
    format_cell_methods,
    format_duration,
    infer_timestep,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10}
RESAMPLE_FREQ = {"daily": "1D", "weekly": "7D", "dekadal": "10D", "monthly": "MS"}
ANCHOR_PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10, "monthly": 30}
_METHOD_TO_CF = {"mean": "mean", "max": "maximum", "min": "minimum"}


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


def _aggregate_time_resample(ds, dim, period, method, *, keep_partial: bool):
    """Forward calendar resample; drop incomplete bins unless ``keep_partial``."""
    import numpy as np
    import xarray as xr

    freq = RESAMPLE_FREQ[period]
    timestep_days = _timestep_days(ds, dim)
    resampler = ds.resample({dim: freq})
    n = ds.sizes[dim]
    chunks, labels = [], []
    dropped = 0
    for label, group in resampler.groups.items():
        indices = _group_indices(group, n)
        if not indices:
            continue
        expected = _expected_samples(period, label, timestep_days)
        if not keep_partial and len(indices) < expected:
            dropped += 1
            continue
        chunks.append(_reduce(ds.isel({dim: indices}), method=method, dim=dim))
        labels.append(np.datetime64(label) if not hasattr(label, "calendar") else label)
    if dropped:
        print(
            f"dropped {dropped} incomplete {period} bin(s); "
            "pass --keep-partial to retain",
            file=sys.stderr,
        )
    if not chunks:
        return ds.isel({dim: slice(0, 0)})
    return xr.concat(chunks, dim=dim).assign_coords({dim: labels})


def _aggregate_time_anchored(
    ds, dim, period, method, end_time, *, keep_partial: bool, start_time=None
):
    """Backward fixed-width bins ending at ``end_time``.

    Walks while the bin's right edge is still after the effective data start,
    then keeps bins that contain samples. Incomplete bins (fewer samples than
    a full period) are dropped unless ``keep_partial``. Optional ``start_time``
    raises the earliest coverage floor.
    """
    import numpy as np
    import xarray as xr

    period_days = ANCHOR_PERIOD_DAYS[period]
    raw = np.asarray(ds[dim].values)
    if raw.size == 0:
        return ds.isel({dim: slice(0, 0)})
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
            right = cftime.datetime(
                end_time.year, end_time.month, end_time.day, calendar=calendar
            )
        except ValueError:
            raise UsageError(
                f"--end-time {end_time.isoformat()} invalid in {calendar!r}"
            ) from None
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
    bins, r = [], right
    # Stop once the right edge is at/before the first sample — do not require
    # left >= data_min, or a bin that still holds data is never created.
    while r > data_min:
        left = r - bin_width
        bins.append((left, r))
        r = left
    bins.reverse()
    chunks, labels = [], []
    dropped = 0
    for left, right in bins:
        keep = np.nonzero((raw > left) & (raw <= right))[0]
        if keep.size == 0:
            continue
        expected = _expected_samples(
            period, right, timestep_days, period_days=period_days
        )
        if not keep_partial and keep.size < expected:
            dropped += 1
            continue
        chunks.append(_reduce(ds.isel({dim: keep}), method=method, dim=dim))
        labels.append(label(right))
    if dropped:
        print(
            f"dropped {dropped} incomplete {period} bin(s); "
            "pass --keep-partial to retain",
            file=sys.stderr,
        )
    if not chunks:
        return ds.isel({dim: slice(0, 0)})
    return xr.concat(chunks, dim=dim).assign_coords({dim: labels})


def _aggregate_step(ds, period, method):
    import numpy as np
    import pandas as pd
    import xarray as xr

    if period == "monthly":
        raise UsageError("monthly aggregation is not defined for step")
    window = pd.Timedelta(days=PERIOD_DAYS[period]).to_timedelta64()
    steps = ds["step"].values
    max_step = steps.max()
    edges = np.arange(0, max_step + window, window, dtype=steps.dtype)
    chunks, labels = [], []
    for left in edges[:-1]:
        right = left + window
        if right > max_step:
            continue
        mask = (steps > left) & (steps <= right)
        if not mask.any():
            continue
        chunks.append(_reduce(ds.isel(step=np.where(mask)[0]), method=method, dim="step"))
        labels.append(left + window)
    return xr.concat(chunks, dim="step").assign_coords(step=labels)


def _stamp_attrs(out, dim, agg_period, method, interval):
    cf_method = _METHOD_TO_CF[method]
    cm = format_cell_methods(dim, cf_method, interval=interval)
    for name in out.data_vars:
        attrs = {**out[name].attrs, AGGREGATION_PERIOD_ATTR: agg_period, "cell_methods": cm}
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
    dest="ds",
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--period", default=None, choices=["daily", "weekly", "dekadal", "monthly"]
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
@weather_skill.argument(
    "--keep-partial",
    action="store_true",
    help=(
        "Keep incomplete calendar bins (e.g. a trailing week with fewer than "
        "7 daily samples). Default drops them so convert-to-totals does not "
        "scale a partial mean up to a full aggregation_period."
    ),
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
    keep_partial,
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
    if window is not None and keep_partial:
        raise UsageError("--keep-partial applies to --period only, not --window")
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

    interval = None
    try:
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
        out = roll_and_agg(ds, window, dim, method, align=align, stride=stride_val)
        agg_period = _rolling_aggregation_period(ds, dim, window, interval)
        return _stamp_attrs(out, dim, agg_period, method, interval)

    if dim == "step":
        # Step buckets already require a full window (right <= max_step).
        out = _aggregate_step(ds, period, method)
        if keep_partial:
            print(
                "--keep-partial has no effect on step aggregation "
                "(incomplete step bins are never emitted)",
                file=sys.stderr,
            )
        if end_time is not None or start_time is not None:
            print(
                "--start-time/--end-time have no effect on step aggregation",
                file=sys.stderr,
            )
    elif end_time is not None:
        out = _aggregate_time_anchored(
            ds,
            dim,
            period,
            method,
            end_time,
            keep_partial=keep_partial,
            start_time=start_time,
        )
    else:
        out = _aggregate_time_resample(
            ds, dim, period, method, keep_partial=keep_partial
        )

    return _stamp_attrs(out, dim, PERIOD_TO_AGGREGATION[period], method, interval)


if __name__ == "__main__":
    aggregate()
