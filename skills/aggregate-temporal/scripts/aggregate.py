# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cf-xarray",
#   "cftime",
#   "xarray",
#   "numpy",
#   "pandas",
# ]
# ///
"""Temporal aggregation: calendar resample, rolling window, or step buckets."""

import datetime as _dt

from weather_skills_core import Types, UsageError, weather_skill
from weather_skills_core.dataset import roll_and_agg

_SKILL_VERSION = "0.1.13"

PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10}
RESAMPLE_FREQ = {"daily": "1D", "weekly": "7D", "dekadal": "10D", "monthly": "MS"}
ANCHOR_PERIOD_DAYS = {"daily": 1, "weekly": 7, "dekadal": 10, "monthly": 30}
_TEMP_UNITS = {
    "k", "degk", "degc", "celsius", "degree_celsius", "degrees_celsius", "degreec", "°c",
    "degf", "degree_fahrenheit", "degrees_fahrenheit", "fahrenheit", "°f",
}
_DEPTH = {"mm": "mm", "kg m**-2": "kg m**-2", "kg m-2": "kg m**-2", "kg m^-2": "kg m**-2"}
_RATE_TO_AMOUNT = {"lwe_precipitation_rate": "lwe_thickness_of_precipitation_amount"}


def _reduce(grouped, method, dim=None):
    fn = {"sum": grouped.sum, "mean": grouped.mean, "max": grouped.max, "min": grouped.min}[method]
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


@weather_skill(
    name="aggregate-temporal",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.ANY],
    optional_args=("variable",),
)
@weather_skill.argument("--period", default=None, choices=["daily", "weekly", "dekadal", "monthly"])
@weather_skill.argument("--window", type=int, default=None, help="Rolling window (mutex with --period).")
@weather_skill.argument("--align", default="left", choices=["left", "right", "center"])
@weather_skill.argument("--stride", default=None)
@weather_skill.argument("--method", default="sum", choices=["sum", "mean", "max", "min"])
@weather_skill.argument("--anchor-end", default=None)
@weather_skill.argument("--time-dim", default=None)
def aggregate(ds, variable, period, window, align, stride, method, anchor_end, time_dim):
    """Temporal aggregation: calendar resample, rolling window, or step buckets."""
    import cf_xarray  # noqa: F401
    import pandas as pd

    if (period is None) == (window is None):
        raise UsageError("exactly one of --period or --window is required")
    if window is not None and anchor_end is not None:
        raise UsageError("--anchor-end cannot be combined with --window")
    if window is None and stride is not None:
        raise UsageError("--stride requires --window")
    if window is None and align != "left":
        raise UsageError("--align requires --window")
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

    # Sum of intensive quantities is silently meaningless
    if method == "sum":
        for var in ds.data_vars:
            sn = ds[var].attrs.get("standard_name")
            u = ds[var].attrs.get("units")
            name = sn.strip().lower() if isinstance(sn, str) else ""
            units = u.strip().lower() if isinstance(u, str) else ""
            intensive = (
                name == "air_temperature"
                or name.endswith("_temperature")
                or units in _TEMP_UNITS
                or (units in {"pa", "hpa", "mbar", "bar"} and "pressure" in name)
                or units in {"%", "percent"}
            )
            if intensive:
                raise UsageError(f"variable '{var}' is intensive; refuse --method sum")

    stride_val = stride
    if stride is not None:
        try:
            stride_val = int(stride)
        except ValueError:
            pass

    if window is not None:
        out = roll_and_agg(ds, window, dim, method, align=align, stride=stride_val)
    elif dim == "step":
        out = _aggregate_step(ds, period, method)
    elif anchor_end is not None:
        out = _aggregate_time_anchored(ds, dim, period, method, pd.Timestamp(anchor_end))
    else:
        out = _reduce(ds.resample({dim: RESAMPLE_FREQ[period]}), method)

    # Relabel mm/day → mm after sum (otherwise units lie)
    if method == "sum":
        for var in out.data_vars:
            units = out[var].attrs.get("units")
            sn = out[var].attrs.get("standard_name")
            if not isinstance(units, str) or not units.strip():
                continue
            u = units.strip()
            if "/" in u:
                head, _, tail = u.rpartition("/")
                ok = {"day", "days", "d"}
            else:
                head, _, tail = u.rpartition(" ")
                ok = {"day-1", "d-1", "day**-1", "d**-1"}
            depth = _DEPTH.get(head.strip().lower()) if tail.strip().lower() in ok else None
            if depth is None:
                continue
            out[var].attrs["units"] = depth
            if isinstance(sn, str) and sn.strip() in _RATE_TO_AMOUNT:
                out[var].attrs["standard_name"] = _RATE_TO_AMOUNT[sn.strip()]
            elif isinstance(sn, str) and sn.strip().lower().endswith(("_rate", "_flux")):
                out[var].attrs.pop("standard_name", None)
    return out


if __name__ == "__main__":
    aggregate()
