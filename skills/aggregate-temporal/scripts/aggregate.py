# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cf-xarray",
#   "cftime",
#   "xarray",
#   "numpy",
#   "pandas",
#   "pint-xarray>=0.6",
# ]
# ///
"""Temporal aggregation: calendar resample or step buckets (rates; mean/min/max)."""

import datetime as _dt

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.units import (
    AGGREGATION_PERIOD_ATTR,
    PERIOD_TO_AGGREGATION,
    format_cell_methods,
    format_duration,
    infer_timestep,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.13"

PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10}
RESAMPLE_FREQ = {"daily": "1D", "weekly": "7D", "dekadal": "10D", "monthly": "MS"}
ANCHOR_PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10, "monthly": 30}
_METHOD_TO_CF = {"mean": "mean", "max": "maximum", "min": "minimum"}


def _reduce(grouped, method, dim=None):
    fn = {"mean": grouped.mean, "max": grouped.max, "min": grouped.min}[method]
    return fn(dim=dim, keep_attrs=True) if dim is not None else fn(keep_attrs=True)


def _aggregate_time_anchored(ds, dim, period, method, anchor_end):
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
        try:
            right = cftime.datetime(
                anchor_end.year, anchor_end.month, anchor_end.day,
                anchor_end.hour, anchor_end.minute, anchor_end.second,
                anchor_end.microsecond, calendar=calendar,
            )
        except ValueError:
            raise UsageError(
                f"--anchor-end {anchor_end.date().isoformat()} invalid in {calendar!r}"
            ) from None
        label = lambda edge: edge  # noqa: E731
    else:
        import pandas as pd

        bin_width = pd.Timedelta(days=period_days)
        data_min = pd.Timestamp(pd.to_datetime(raw).min())
        right = pd.Timestamp(anchor_end)
        label = lambda edge: np.datetime64(edge)  # noqa: E731

    bins, r = [], right
    while True:
        left = r - bin_width
        if left < data_min:
            break
        bins.append((left, r))
        r = left
    bins.reverse()
    chunks, labels = [], []
    for left, right in bins:
        keep = np.nonzero((raw > left) & (raw <= right))[0]
        if keep.size == 0:
            continue
        chunks.append(_reduce(ds.isel({dim: keep}), method=method, dim=dim))
        labels.append(label(right))
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


def _stamp_aggregation_metadata(out, dim, period, method, interval):
    agg_period = PERIOD_TO_AGGREGATION[period]
    cf_method = _METHOD_TO_CF[method]
    cm = format_cell_methods(dim, cf_method, interval=interval)
    for name in out.data_vars:
        attrs = {**out[name].attrs, AGGREGATION_PERIOD_ATTR: agg_period, "cell_methods": cm}
        out[name].attrs = attrs
    return out


@weather_skill(
    name="aggregate-temporal",
    version=_SKILL_VERSION,
    inputs=[["time", "prediction_timedelta"]],
    outputs=["any"],
)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--period", required=True, choices=["daily", "weekly", "dekadal", "monthly"]
)
@weather_skill.argument("--method", default="mean", choices=["mean", "max", "min"])
@weather_skill.argument("--time-dim", default=None)
@weather_skill.argument("--anchor-end", default=None)
def aggregate(ds, variable, time_dim, period, method, anchor_end, **kwargs):
    """Temporal aggregation of rates: calendar resample or step buckets."""
    import cf_xarray  # noqa: F401
    import pandas as pd

    if anchor_end is not None:
        try:
            _dt.date.fromisoformat(anchor_end)
        except ValueError as exc:
            raise UsageError(f"--anchor-end '{anchor_end}' not YYYY-MM-DD: {exc}") from None

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

    if dim == "step":
        out = _aggregate_step(ds, period, method)
    elif anchor_end is not None:
        out = _aggregate_time_anchored(ds, dim, period, method, pd.Timestamp(anchor_end))
    else:
        out = _reduce(ds.resample({dim: RESAMPLE_FREQ[period]}), method)

    return _stamp_aggregation_metadata(out, dim, period, method, interval)


if __name__ == "__main__":
    aggregate()
