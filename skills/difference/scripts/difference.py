# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Subtract A − B (xarray-aligned)."""

from weather_skills_core import weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.7"

_WIDEN = {1: "int16", 2: "int32", 4: "int64"}


@weather_skill(
    name="difference",
    version=_SKILL_VERSION,
    inputs=["any", "any"],
    outputs=["any"],
)
@weather_skill.argument("--variable", "-v", action="append")
def difference(ds_a, ds_b, variable, **kwargs):
    """Subtract A − B (xarray-aligned)."""
    import numpy as np
    import xarray as xr

    vars_ = list(dict.fromkeys(variable)) if variable else [
        v for v in ds_a.data_vars if v in ds_b.data_vars
    ]
    out = {}
    for v in vars_:
        a, b = ds_a[v], ds_b[v]
        # Promote bool/uint so A−B cannot wrap
        if a.dtype.kind == "b":
            a = a.astype(np.int16)
        elif a.dtype.kind == "u":
            a = a.astype(getattr(np, _WIDEN.get(a.dtype.itemsize, "int64")))
        if b.dtype.kind == "b":
            b = b.astype(np.int16)
        elif b.dtype.kind == "u":
            b = b.astype(getattr(np, _WIDEN.get(b.dtype.itemsize, "int64")))
        out[v] = (a - b).assign_attrs(ds_a[v].attrs)
    return xr.Dataset(out)


if __name__ == "__main__":
    difference()
