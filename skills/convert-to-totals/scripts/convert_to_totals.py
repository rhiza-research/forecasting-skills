# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cf-xarray",
#   "cftime>=1.6",
#   "pint-xarray>=0.6",
# ]
# ///
"""Convert rate variables to period totals using stamped aggregation_period."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    PRECIP_AMOUNT_LONG_NAME,
    STANDARD,
    assert_nonoverlapping_intervals,
    classify_variable,
    filter_min_coverage,
    format_cell_methods,
    looks_like_rate_display_name,
    rate_to_total,
    variable_units,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _resolve_dim(ds, time_dim):
    import cf_xarray  # noqa: F401

    if time_dim:
        if time_dim not in ds.dims:
            raise UsageError(f"--time-dim {time_dim!r} not in dataset (have {list(ds.dims)})")
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
    return None


def _sum_cell_methods_dim(da, dim: str | None) -> str:
    """Dim name for ``cell_methods: …: sum`` after conversion."""
    if dim:
        return dim
    methods = da.attrs.get("cell_methods")
    if isinstance(methods, str):
        for token in ("time", "step"):
            if f"{token}:" in methods:
                return token
    return "time"


@weather_skill(
    name="convert-to-totals",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--min-coverage",
    type=float,
    default=1.0,
    help="Drop intervals whose aggregation_coverage is below this (0–1). Default 1.0.",
)
@weather_skill.argument("--time-dim", default=None)
def convert_to_totals(ds, variable, min_coverage, time_dim, **kwargs):
    """Multiply rates by stamped aggregation_period → amounts. Terminal before plot."""
    dim = _resolve_dim(ds, time_dim)
    names = list(dict.fromkeys(variable)) if variable is not None else list(ds.data_vars)
    for name in names:
        if name not in ds.data_vars:
            raise UsageError(f"variable {name!r} not in dataset (have {list(ds.data_vars)})")

    # After select collapses the only time/step sample, dims are spatial-only.
    # aggregation_period on the variable is enough for the multiply; skip the
    # coverage/overlap gates that need a time axis.
    if dim is not None:
        ds = filter_min_coverage(ds, dim, min_coverage)
    out = ds.copy(deep=False)
    for name in names:
        da = ds[name]
        period = da.attrs.get(AGGREGATION_PERIOD_ATTR)
        if not (isinstance(period, str) and period.strip()):
            raise UsageError(
                f"variable {name!r} has no {AGGREGATION_PERIOD_ATTR!r}; "
                "run aggregate-temporal first"
            )
        if dim is not None:
            assert_nonoverlapping_intervals(ds, dim, period)
        total = rate_to_total(da, period)
        plain = total.pint.dequantify() if total.pint.units is not None else total
        attrs = {**da.attrs, **plain.attrs}
        attrs["units"] = plain.attrs.get("units", STANDARD["precip_amount"]["units"])
        units = variable_units(da) or da.attrs.get("units")
        kind = classify_variable(name, units=units, standard_name=da.attrs.get("standard_name"))
        if kind in ("precip", "precip_amount") or (
            isinstance(name, str) and any(h in name.lower() for h in ("precip", "rain", "tp", "pr"))
        ):
            attrs["units"] = STANDARD["precip_amount"]["units"]
            attrs["standard_name"] = STANDARD["precip_amount"]["standard_name"]
            attrs["long_name"] = PRECIP_AMOUNT_LONG_NAME
            if looks_like_rate_display_name(attrs.get("GRIB_name")):
                attrs["GRIB_name"] = PRECIP_AMOUNT_LONG_NAME
        attrs["cell_methods"] = format_cell_methods(_sum_cell_methods_dim(da, dim), "sum")
        # Keep aggregation_period so plotters can pick a period-aware precip palette.
        out[name] = plain
        out[name].attrs = attrs
    if AGGREGATION_COVERAGE_COORD in out.coords:
        out = out.drop_vars(AGGREGATION_COVERAGE_COORD)
    return out


if __name__ == "__main__":
    convert_to_totals()
