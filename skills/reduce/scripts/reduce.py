# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
# ]
# ///
"""Collapse one or more named dimensions of a weather-skills envelope Zarr with a statistic.

Reduces the selected data variables along the requested dims with one of
``mean``/``std``/``min``/``max``/``sum``/``median`` (NaNs are skipped), e.g.
the ensemble-spread field as the std across ``number``, model disagreement as
the std across a model dim, or a time-mean baseline for anomaly computation.
Data variables that carry none of the requested dims pass through untouched.
"""

import sys

from weather_skills_core import UsageError, WroteSummary, input_path, types, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.7"


def _normalize_args(args):
    # Normalize provenance args before stamping so reordered or duplicated
    # flags don't cause spurious cache misses: dedupe + sort --dim, dedupe
    # --variable (order-insensitive selection, but keep them as a sorted list
    # so the recorded args are canonical).
    args["dim"] = sorted(set(args["dim"]))
    if args.get("variable") is not None:
        args["variable"] = sorted(set(args["variable"]))
    return args


@weather_skill(
    "reduce",
    _SKILL_VERSION,
    input_type=types.ALL,
    # The output shape depends on the input's shape and the collapsed dims,
    # so the union allows every envelope shape; the returned dataset's
    # detected shape is validated against it before the write.
    output_type=types.ALL,
    variable={
        "mode": types.REPEAT,
        "help": "Restrict the reduction to this data variable. Repeat once per "
        "variable to select several; each selected variable must carry every "
        "requested --dim. Default (unset) reduces every data variable that "
        "carries at least one of the requested dims. Unselected or "
        "untouched data variables pass through unchanged.",
    },
    extra_args={
        "dim": {
            "repeat": True,
            "required": True,
            "help": "Dimension to collapse. Repeat once per dimension to collapse "
            "several in one run.",
        },
        "method": {
            "required": True,
            "choices": ["mean", "std", "min", "max", "sum", "median"],
            "help": "Statistic applied along the collapsed dimension(s).",
        },
    },
    normalize_args=_normalize_args,
)
def reduce(ds, variable, dim, method):
    """Collapse one or more named dimensions of a weather-skills envelope Zarr with a statistic."""
    src = input_path(ds)

    # De-duplicate the requested dims preserving first-seen order so a
    # repeated name doesn't reduce twice; each must be an actual dim.
    dims = list(dict.fromkeys(dim))
    invalid_dims = [d for d in dims if d not in ds.dims]
    if invalid_dims:
        raise UsageError(f"--dim {invalid_dims} not in dims {list(ds.dims)}.")

    # Variable selection. Explicit --variable names must be data variables and
    # must each carry every requested dim. Default selection takes every data
    # variable carrying at least one of the requested dims (each is reduced
    # over the subset of dims it carries); the rest pass through untouched.
    if variable is not None:
        data_vars = list(ds.data_vars)
        invalid_vars = [v for v in variable if v not in ds.data_vars]
        if invalid_vars:
            raise UsageError(
                f"--variable {invalid_vars} not data variable(s) of {src}. "
                f"Valid data variables: {data_vars}"
            )
        # De-duplicate while preserving first-seen order so a repeated name
        # doesn't reduce a variable twice.
        selected = list(dict.fromkeys(variable))
        for var in selected:
            missing = [d for d in dims if d not in ds[var].dims]
            if missing:
                raise UsageError(
                    f"variable '{var}' does not carry --dim {missing}; "
                    f"its dims are {list(ds[var].dims)}."
                )
    else:
        selected = [v for v in ds.data_vars if any(d in ds[v].dims for d in dims)]
        if not selected:
            detail = ", ".join(f"{v}{tuple(ds[v].dims)}" for v in ds.data_vars)
            raise UsageError(
                f"no data variable carries any of --dim {dims}. "
                f"Data variables and their dims: {detail}."
            )

    passthrough = [v for v in ds.data_vars if v not in selected]
    if passthrough:
        print(
            f"Note: passing through unreduced data variable(s) {passthrough}.",
            file=sys.stderr,
        )

    print(
        f"Reducing dims={dims} method={method} variables={selected}",
        file=sys.stderr,
    )

    # Reduce each selected variable over the requested dims it carries.
    # keep_attrs=True preserves the variable attrs (units included); this
    # skill performs no unit math or relabeling — `sum` keeps the input
    # units attr unchanged, and unit-convert exists to restamp units when
    # needed. NaNs are skipped (xarray's default skipna).
    out_ds = ds.copy()
    for var in selected:
        da = ds[var]
        rdims = [d for d in dims if d in da.dims]

        # `std` over a size-1 dim is zero by construction (a single sample has
        # no spread); warn so a degenerate spread field isn't read as real.
        if method == "std":
            singleton = [d for d in rdims if da.sizes[d] == 1]
            if singleton:
                print(
                    f"Warning: std of variable '{var}' over size-1 dim(s) "
                    f"{singleton} is zero by construction (a single sample has "
                    "no spread).",
                    file=sys.stderr,
                )

        # `median` over ALL of a dask-backed variable's dims raises
        # NotImplementedError in dask (no flattened-median across chunks), so
        # materialize the variable first. Only the all-dims case is affected;
        # a partial-dims median streams fine.
        if method == "median" and da.chunks is not None and set(rdims) == set(da.dims):
            da = da.load()

        if method == "sum":
            # min_count=1 keeps an all-missing slice NaN instead of summing to
            # 0 (which would read as a real zero total rather than "no data").
            out_ds[var] = da.sum(dim=rdims, keep_attrs=True, min_count=1)
        elif method == "std":
            # ddof=1 is the sample standard deviation (ensemble-spread
            # convention: spread across members is a sample estimate, not the
            # population sigma).
            out_ds[var] = da.std(dim=rdims, keep_attrs=True, ddof=1)
        else:
            fn = {
                "mean": da.mean,
                "min": da.min,
                "max": da.max,
                "median": da.median,
            }[method]
            out_ds[var] = fn(dim=rdims, keep_attrs=True)

    # A reduced dim disappears from the reduced variables, but its index
    # coordinate (and any auxiliary coordinate on the dim) would otherwise
    # keep the dim alive on the dataset. Drop each requested dim once no
    # data variable carries it; a dim still carried by a pass-through
    # variable stays.
    for d in dims:
        if d in out_ds.dims and all(d not in out_ds[v].dims for v in out_ds.data_vars):
            out_ds = out_ds.drop_dims(d)

    return out_ds, WroteSummary(f"{out_ds.sizes}", replace=True)


if __name__ == "__main__":
    reduce()
