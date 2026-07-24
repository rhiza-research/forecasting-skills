# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "numpy>=2.4",
# ]
# ///
"""Deaccumulate a cumulative-since-init variable along the forecast step axis.

Some forecast variables (e.g. ECMWF S2S ``tp``, surface radiation, evaporation,
SWE) are stored as values accumulated from the forecast initialization time.
This skill converts those to per-step differences: ``out[i] = arr[i+1] - arr[i]``,
clipped at zero. The output ``step`` coord drops the first input step, so the
resulting axis labels each value with the end of the period it covers.
"""

import re
import sys

from weather_skills_core import UsageError, WroteSummary, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"

# Time-unit tokens that, when they appear as a per-time denominator, mark a
# rate. Deliberately excludes length/mass/etc. tokens (e.g. ``m``) so that a
# per-AREA term such as ``m-2`` / ``m**-2`` in an accumulated depth like
# ``kg m**-2`` is never read as a rate.
_TIME_UNIT_TOKENS = ("second", "sec", "minute", "min", "hour", "hr", "day", "s", "h", "d")

# Slash-denominator rate forms: a ``/`` immediately followed by a time-unit
# token (tolerating surrounding spaces), e.g. ``mm/day``, ``m / s``. Anchored
# on a word boundary at the end of the token so ``/day`` does not also match a
# longer word that merely starts with the token.
_RATE_SLASH_RE = re.compile(
    r"/\s*(?:" + "|".join(_TIME_UNIT_TOKENS) + r")\b",
    re.IGNORECASE,
)

# UDUNITS negative-power rate forms on a time-unit token, restricted to the
# first negative power: ``s-1``, ``s**-1``, ``s^-1``, ``day-1``, etc. The token
# must stand alone (word boundary before it) so that the ``-2`` in ``m-2`` /
# ``m**-2`` (per area) is never matched and the ``m`` length token never
# participates. The power is fixed at exactly ``-1`` (a per-time rate); higher
# negative powers such as ``s-2`` denote acceleration / energy-per-mass (e.g.
# the ``m2 s-2`` of geopotential or CAPE), which are not rates.
_RATE_POWER_RE = re.compile(
    r"\b(?:" + "|".join(_TIME_UNIT_TOKENS) + r")(?:\*\*|\^)?-1\b",
    re.IGNORECASE,
)

# A standalone watt token marks a power (energy per time), which is inherently a
# per-time rate (``W`` = J/s). So a units string such as ``W m-2`` / ``W m**-2``
# / ``W/m2`` (instantaneous radiation flux) is a rate, in contrast to its
# accumulated form ``J m-2``. Match the SI symbol ``W`` on word boundaries (so
# it does not fire inside another token like ``Wb``) or the spelled-out
# ``watt``/``watts``. Under IGNORECASE this also matches a lone lowercase ``w``,
# which is acceptable (there is no standard unit ``w``).
_RATE_WATT_RE = re.compile(
    r"\b(?:W|watts?)\b",
    re.IGNORECASE,
)


def _units_look_like_rate(units: str) -> bool:
    """Return True when a CF ``units`` string carries a per-time denominator or a
    power (watt) term, indicating a rate rather than an accumulated amount.

    Detects three forms:
      - a slash denominator on a time-unit token (``s``, ``sec``, ``second``,
        ``min``, ``minute``, ``h``, ``hr``, ``hour``, ``d``, ``day``):
        ``mm/day``, ``m / s``;
      - a UDUNITS first negative power on a time-unit token: ``s-1``, ``s**-1``,
        ``s^-1``, ``day-1``;
      - a standalone watt token (``W``, ``watt``, ``watts``): ``W m-2``,
        ``W m**-2``, ``W/m2``.

    So ``mm/day``, ``kg m-2 s-1``, ``m s-1``, and ``W m-2`` are rates, while the
    per-area ``m-2`` / ``m**-2`` in ``kg m**-2`` is not (``m`` is not a time
    token), and the higher negative power ``m2 s-2`` (acceleration /
    energy-per-mass) is not a rate.
    """
    if not units:
        return False
    return bool(
        _RATE_SLASH_RE.search(units) or _RATE_POWER_RE.search(units) or _RATE_WATT_RE.search(units)
    )


@weather_skill(
    "deaccumulate",
    _SKILL_VERSION,
    input_type="any",
    output_type="same",
    variable={
        "mode": "single",
        "help": "Variable to deaccumulate. Required if the input has multiple data vars.",
    },
    hash_input=False,
)
def deaccumulate(ds, variable):
    """Deaccumulate a cumulative-since-init variable along the forecast step axis."""
    import numpy as np

    if "step" not in ds.dims:
        raise UsageError(f"input has no 'step' dim; got dims {list(ds.dims)}.")

    data_vars = list(ds.data_vars)
    if variable:
        if variable not in ds.data_vars:
            raise UsageError(f"variable '{variable}' not in data_vars {data_vars}.")
    elif len(data_vars) == 1:
        variable = data_vars[0]
    else:
        raise UsageError(f"input has multiple data vars {data_vars}; specify --variable.")

    da = ds[variable]
    if da.sizes["step"] < 2:
        raise UsageError(f"'step' dim has length {da.sizes['step']}; need at least 2 to diff.")

    # Input-units guard. Deaccumulation differences the variable along step and
    # is only meaningful for a cumulative-since-init accumulated quantity (a
    # depth/amount that grows along step). A per-time rate must not be
    # deaccumulated: differencing a rate yields a meaningless difference-of-rates
    # still labeled with the rate's units. Reject when the metadata positively
    # indicates a rate; warn but proceed when there is no metadata to check.
    units = da.attrs.get("units")
    standard_name = da.attrs.get("standard_name")
    if units is None and standard_name is None:
        print(
            f"Warning: variable '{variable}' has no 'units' or 'standard_name'; "
            "cannot validate that the input is an accumulated quantity. Proceeding.",
            file=sys.stderr,
        )
    else:
        name_is_rate = isinstance(standard_name, str) and standard_name.strip().lower().endswith(
            ("_rate", "_flux")
        )
        units_are_rate = isinstance(units, str) and _units_look_like_rate(units)
        if name_is_rate or units_are_rate:
            raise UsageError(
                f"variable '{variable}' looks like a per-time rate "
                f"(units={units!r}, standard_name={standard_name!r}); refusing to "
                "deaccumulate. deaccumulate expects a cumulative-since-init "
                "accumulated quantity (a depth/amount such as 'kg m**-2', 'm', or "
                "'mm') that grows along step. A per-time rate (e.g. a CHIRPS/IMERG "
                "daily 'mm/day' product) is already per-period and must not be "
                "deaccumulated."
            )

    diffed = da.isel(step=slice(1, None)).copy(
        data=np.clip(
            da.isel(step=slice(1, None)).values - da.isel(step=slice(0, -1)).values,
            a_min=0,
            a_max=None,
        )
    )
    diffed.attrs = dict(da.attrs)

    out_ds = ds.drop_vars(variable).isel(step=slice(1, None))
    out_ds[variable] = diffed
    return out_ds, WroteSummary(
        f"variable={variable}, step length {da.sizes['step']} -> {out_ds.sizes['step']}",
        replace=True,
    )


if __name__ == "__main__":
    deaccumulate()
