# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Subtract A − B (xarray-aligned)."""

from weather_skills_core import Types, weather_skill

_SKILL_VERSION = "0.1.7"


def _signed(da, np):
    """Promote bool/uint so A−B cannot wrap."""
    if da.dtype.kind == "b":
        return da.astype(np.int16)
    if da.dtype.kind == "u":
        return da.astype({1: np.int16, 2: np.int32, 4: np.int64}.get(da.dtype.itemsize, np.int64))
    return da


@weather_skill(
    name="difference",
    version=_SKILL_VERSION,
    inputs=[Types.ANY, Types.ANY],
    outputs=[Types.ANY],
    optional_args=("variable",),
)
def difference(ds_a, ds_b, variable):
    """Subtract A − B (xarray-aligned)."""
    import numpy as np
    import xarray as xr

    vars_ = variable or [v for v in ds_a.data_vars if v in ds_b.data_vars]
    return xr.Dataset(
        {
            v: (_signed(ds_a[v], np) - _signed(ds_b[v], np)).assign_attrs(ds_a[v].attrs)
            for v in vars_
        }
    )


if __name__ == "__main__":
    difference()
