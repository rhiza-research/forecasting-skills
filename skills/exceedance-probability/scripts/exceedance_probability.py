# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
# ]
# ///
"""Compute the percentage of ensemble members exceeding a fixed threshold.

For each selected data variable, computes the percentage of entries along
``--dim`` (e.g. ``number`` for an ECMWF/GEFS ensemble) satisfying
``value <comparison> threshold``, per remaining grid cell/step. Data
variables that don't carry ``--dim`` pass through untouched.
"""

import operator
import sys

from weather_skills_core import UsageError, WroteSummary, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"

_COMPARISONS = {
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
}


def _normalize_args(args):
    # Normalize provenance args before stamping so reordered or duplicated
    # --variable flags don't cause spurious cache misses; --dim, --threshold,
    # and --comparison are already scalars and need no normalization.
    if args.get("variable") is not None:
        args["variable"] = sorted(set(args["variable"]))
    return args


@weather_skill(
    "exceedance-probability",
    _SKILL_VERSION,
    input_type="any",
    # The collapsed dim isn't fixed to one envelope shape (forecast `number`
    # is the ECMWF case, but nothing stops reuse on a station ensemble or
    # other member-like dim), so the union declares every zarr envelope
    # shape; the returned dataset's detected shape is validated against it
    # before the write.
    output_type=("gridded", "forecast", "station"),
    input_paths=True,
    variable={
        "mode": "repeat",
        "help": "Restrict the computation to this data variable. Repeatable. "
        "Each selected variable must carry --dim. Default (unset): every "
        "data variable carrying --dim.",
    },
    extra_args={
        "dim": {
            "required": True,
            "help": "Ensemble/member dimension to compute the percentage over "
            "(e.g. 'number' for ECMWF/GEFS forecast envelopes).",
        },
        "threshold": {
            "required": True,
            "type": float,
            "help": "Value to compare each member against, in the variable's own units.",
        },
        "comparison": {
            "required": True,
            "choices": ["gt", "ge", "lt", "le"],
            "help": "Comparison applied as value <op> threshold.",
        },
    },
    normalize_args=_normalize_args,
)
def exceedance_probability(ds, input_paths, variable, dim, threshold, comparison):
    """Compute the percentage of ensemble members exceeding a fixed threshold."""
    src = input_paths[0]

    if dim not in ds.dims:
        raise UsageError(f"--dim '{dim}' not in dims {list(ds.dims)}.")

    # Variable selection, mirroring `reduce`: explicit --variable names must
    # be data variables and must each carry --dim. Default selection takes
    # every data variable carrying --dim; the rest pass through untouched.
    if variable is not None:
        data_vars = list(ds.data_vars)
        invalid = [v for v in variable if v not in ds.data_vars]
        if invalid:
            raise UsageError(
                f"--variable {invalid} not data variable(s) of {src}. "
                f"Valid data variables: {data_vars}"
            )
        selected = list(dict.fromkeys(variable))
        missing = [v for v in selected if dim not in ds[v].dims]
        if missing:
            raise UsageError(f"variable(s) {missing} do not carry --dim '{dim}'.")
    else:
        selected = [v for v in ds.data_vars if dim in ds[v].dims]
        if not selected:
            raise UsageError(f"no data variable carries --dim '{dim}'.")

    passthrough = [v for v in ds.data_vars if v not in selected]
    if passthrough:
        print(
            f"Note: passing through unreduced data variable(s) {passthrough}.",
            file=sys.stderr,
        )

    print(
        f"Computing exceedance probability dim={dim} comparison={comparison} "
        f"threshold={threshold} variables={selected}",
        file=sys.stderr,
    )

    comp = _COMPARISONS[comparison]
    out_ds = ds.copy()
    for var in selected:
        da = ds[var]
        condition_met = comp(da, threshold)
        pct = condition_met.sum(dim=dim) / da.sizes[dim] * 100
        src_units = da.attrs.get("units", "")
        label = f"probability {var} {comparison} {threshold}{src_units}"
        # Attrs are rebuilt from scratch, NOT carried over from the source
        # variable: the source's standard_name/long_name describe the input
        # physical quantity, not this derived percentage, and neither
        # survives the unit change to `%`. No standard_name is set — CF has
        # no entry for "probability of exceeding a threshold" to verify
        # against. long_name is set (not just GRIB_name) because `plot`'s
        # colorbar-label resolution checks long_name first.
        pct.attrs = {
            "GRIB_name": label,
            "long_name": label,
            "units": "%",
        }
        out_ds[var] = pct

    # The collapsed dim disappears from the output (with its coordinates)
    # once no data variable carries it; a dim still carried by a
    # pass-through variable stays.
    if dim in out_ds.dims and all(dim not in out_ds[v].dims for v in out_ds.data_vars):
        out_ds = out_ds.drop_dims(dim)

    return out_ds, WroteSummary(f"{out_ds.sizes}", replace=True)


if __name__ == "__main__":
    exceedance_probability()
