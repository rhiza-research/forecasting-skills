# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "cftime>=1.6",
#   "pint-xarray>=0.6",
# ]
# ///
"""Convert rate variables to period totals using aggregation_period (terminal for plots)."""

from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    AGGREGATION_PERIOD_ATTR,
    STANDARD,
    assert_timestep_ge_aggregation_period,
    classify_variable,
    format_cell_methods,
    rate_to_total,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


def _resolve_dim(ds, time_dim):
    import cf_xarray  # noqa: F401

    if time_dim:
        return time_dim
    try:
        cf_time = ds.cf["time"].name
    except KeyError:
        cf_time = "time" if "time" in ds.dims else None
    if cf_time is not None and cf_time in ds.dims:
        if ds.sizes[cf_time] == 1 and "step" in ds.dims:
            return "step"
        return cf_time
    if "step" in ds.dims:
        return "step"
    raise UsageError(f"no time/step dim in {list(ds.dims)}; pass --time-dim")


@weather_skill(
    name="convert-to-totals",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset('any'), required=True, dest='ds')
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--aggregation-period",
    default=None,
    help="Override aggregation_period (pint duration, e.g. '7 day').",
)
@weather_skill.argument("--time-dim", default=None)
def convert_to_totals(ds, variable, aggregation_period, time_dim, **kwargs):
    """Multiply rates by aggregation_period → amounts. Terminal before plot."""
    dim = _resolve_dim(ds, time_dim)
    names = list(dict.fromkeys(variable)) if variable is not None else list(ds.data_vars)
    for name in names:
        if name not in ds.data_vars:
            raise UsageError(f"variable {name!r} not in dataset (have {list(ds.data_vars)})")

    out = ds.copy(deep=False)
    for name in names:
        da = ds[name]
        period = aggregation_period or da.attrs.get(AGGREGATION_PERIOD_ATTR)
        if not (isinstance(period, str) and period.strip()):
            raise UsageError(
                f"variable {name!r} has no {AGGREGATION_PERIOD_ATTR!r}; "
                "pass --aggregation-period or run aggregate-temporal first"
            )
        assert_timestep_ge_aggregation_period(ds, dim, period)
        total = rate_to_total(da, period)
        plain = total.pint.dequantify() if total.pint.units is not None else total
        attrs = {**da.attrs, **plain.attrs}
        attrs["units"] = plain.attrs.get("units", STANDARD["precip_amount"]["units"])
        units = variable_units(da) or da.attrs.get("units")
        kind = classify_variable(
            name, units=units, standard_name=da.attrs.get("standard_name")
        )
        if kind in ("precip", "precip_amount") or (
            isinstance(name, str)
            and any(h in name.lower() for h in ("precip", "rain", "tp", "pr"))
        ):
            attrs["units"] = STANDARD["precip_amount"]["units"]
            attrs["standard_name"] = STANDARD["precip_amount"]["standard_name"]
        attrs["cell_methods"] = format_cell_methods(dim, "sum")
        attrs.pop(AGGREGATION_PERIOD_ATTR, None)
        out[name] = plain
        out[name].attrs = attrs
    return out


if __name__ == "__main__":
    convert_to_totals()
