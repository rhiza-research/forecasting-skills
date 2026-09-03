"""Evaluate an IndicatorSpec on a daily DataArray (per-member, then reduce)."""

from __future__ import annotations

import numpy as np
import xarray as xr
from weather_skills_core import UsageError

_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def evaluate_spec(ds, spec, dim: str, variable_override: str | None) -> xr.DataArray:
    """Return a 0/1/NaN mask aligned to ``dim`` (ensemble ``number`` kept)."""
    masks = []
    for clause in spec.clauses:
        name = variable_override or clause.variable
        if name not in ds:
            raise UsageError(
                f"variable {name!r} missing from --input. Available: {list(ds.data_vars)}"
            )
        masks.append(_evaluate_clause(ds[name], clause, dim))
    out = masks[0]
    combine = spec.combinator or "and"
    for other in masks[1:]:
        out = _combine(out, other, combine)
    return out.astype("float32")


def apply_reductions(
    mask: xr.DataArray,
    dim: str,
    *,
    cumulative: bool,
    detect: str | None,
    probability: bool,
) -> xr.Dataset:
    """Apply ``--cumulative``, ``--detect``, ``--probability`` in that order."""
    if detect is not None and cumulative:
        raise UsageError("--detect cannot be combined with --cumulative")
    if detect == "first" and probability:
        raise UsageError(
            "--detect first writes dates, not 0/1; use --detect any --probability "
            "or --cumulative --probability"
        )

    field = mask
    if cumulative:
        field = _cumulative(field, dim)

    if detect == "first":
        return _detect_first(field, dim)
    if detect == "any":
        field = _detect_any(field, dim)

    if probability:
        field = _probability(field)
        name = "probability"
    else:
        name = "indicator"
    field = field.astype("float32")
    field.name = name
    return field.to_dataset()


def _strip_pint(da: xr.DataArray) -> xr.DataArray:
    if getattr(getattr(da, "pint", None), "units", None) is not None:
        return da.pint.dequantify()
    return da


def _evaluate_clause(da: xr.DataArray, clause, dim: str) -> xr.DataArray:
    da = _strip_pint(da)
    core = _core_mask(da, clause, dim)
    if clause.after is not None:
        core = core.shift({dim: -int(clause.after)})
    if clause.within is not None:
        core = _within(core, dim, int(clause.within))
    if clause.negate:
        core = xr.where(core.isnull(), np.nan, 1.0 - core)
    return core.astype("float32")


def _core_mask(da: xr.DataArray, clause, dim: str) -> xr.DataArray:
    agg = clause.agg
    if agg in ("sum", "mean"):
        rolled = _left_roll(da, dim, clause.window, agg)
        compared = _OPS[clause.op](rolled, clause.threshold)
        return xr.where(rolled.isnull(), np.nan, compared.astype("float32"))
    daily = clause.daily_threshold
    if agg == "count-above":
        flag = xr.where(da.isnull(), np.nan, (da > daily).astype("float32"))
        rolled = _left_roll(flag, dim, clause.window, "sum")
        compared = _OPS[clause.op](rolled, clause.threshold)
        return xr.where(rolled.isnull(), np.nan, compared.astype("float32"))
    if agg == "count-below":
        flag = xr.where(da.isnull(), np.nan, (da < daily).astype("float32"))
        rolled = _left_roll(flag, dim, clause.window, "sum")
        compared = _OPS[clause.op](rolled, clause.threshold)
        return xr.where(rolled.isnull(), np.nan, compared.astype("float32"))
    if agg == "consecutive-above":
        flag = xr.where(da.isnull(), np.nan, (da > daily).astype("float32"))
        rolled = _left_roll(flag, dim, clause.window, "sum")
        return xr.where(rolled.isnull(), np.nan, (rolled == clause.window).astype("float32"))
    if agg == "consecutive-below":
        flag = xr.where(da.isnull(), np.nan, (da < daily).astype("float32"))
        rolled = _left_roll(flag, dim, clause.window, "sum")
        return xr.where(rolled.isnull(), np.nan, (rolled == clause.window).astype("float32"))
    raise UsageError(f"unsupported agg {agg!r}")


def _left_roll(da: xr.DataArray, dim: str, window: int, method: str) -> xr.DataArray:
    """Trailing rolling window, then shift labels to the left edge (forward window)."""
    if window < 1:
        raise UsageError(f"rolling window must be >= 1; got {window}")
    axis = da[dim]
    if window == 1:
        return da
    rolled = da.rolling({dim: window}, min_periods=window, center=False)
    if method == "sum":
        out = rolled.sum(skipna=False)
    elif method == "mean":
        out = rolled.mean(skipna=False)
    else:
        raise UsageError(f"unsupported rolling method {method!r}")
    out = out.isel({dim: slice(window - 1, None)})
    step_shift = window - 1
    dtype = axis.dtype
    if np.issubdtype(dtype, np.timedelta64):
        steps = np.asarray(axis.values)
        diffs = np.diff(steps.astype("timedelta64[ns]").astype(np.int64))
        median_ns = int(np.median(diffs)) if diffs.size else 0
        shift = np.timedelta64(step_shift * median_ns, "ns")
    elif np.issubdtype(dtype, np.datetime64):
        shift = np.timedelta64(step_shift, "D")
    else:
        raise UsageError(
            f"rolling requires datetime64 or timedelta64 on {dim!r}; got dtype {dtype}"
        )
    out = out.assign_coords({dim: out[dim] - shift})
    return out.reindex({dim: axis})


def _within(mask: xr.DataArray, dim: str, days: int) -> xr.DataArray:
    """True if ``mask`` is True on any of the next ``days`` labels; NaN if incomplete."""
    if days < 1:
        raise UsageError(f"within window must be >= 1d; got {days}")
    shifted = [mask.shift({dim: -k}) for k in range(1, days + 1)]
    stack = xr.concat(shifted, dim="_look")
    complete = stack.notnull().all("_look")
    hit = (stack == 1).any("_look")
    return xr.where(complete, hit.astype("float32"), np.nan)


def _combine(a: xr.DataArray, b: xr.DataArray, how: str) -> xr.DataArray:
    a, b = xr.align(a, b, join="outer")
    if how == "and":
        both_true = (a == 1) & (b == 1)
        any_false = (a == 0) | (b == 0)
        return xr.where(both_true, 1.0, xr.where(any_false, 0.0, np.nan))
    if how == "or":
        any_true = (a == 1) | (b == 1)
        both_false = (a == 0) & (b == 0)
        return xr.where(any_true, 1.0, xr.where(both_false, 0.0, np.nan))
    raise UsageError(f"unsupported combinator {how!r}")


def _cumulative(mask: xr.DataArray, dim: str) -> xr.DataArray:
    seen = mask.fillna(0).cumsum(dim) > 0
    return xr.where(seen, 1.0, mask).astype("float32")


def _detect_any(mask: xr.DataArray, dim: str) -> xr.DataArray:
    finite = mask.notnull().any(dim)
    hit = (mask == 1).any(dim)
    return hit.astype("float32").where(finite)


def _detect_first(mask: xr.DataArray, dim: str) -> xr.Dataset:
    filled = mask.fillna(0)
    any_true = (mask == 1).any(dim)
    first = filled.idxmax(dim).where(any_true)
    first.name = "indicator_time"
    out = first.to_dataset()
    if np.issubdtype(mask[dim].dtype, np.datetime64):
        doy = first.dt.dayofyear.astype("float32")
        out["indicator_doy"] = doy.where(any_true)
    return out


def _probability(mask: xr.DataArray) -> xr.DataArray:
    if "number" not in mask.dims:
        return mask.astype("float32")
    return mask.mean("number", skipna=True).astype("float32")
