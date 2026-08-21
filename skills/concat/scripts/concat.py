# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime",
#   "xarray",
# ]
# ///
"""Concatenate Zarr stores along a named dim."""

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _coerce(values):
    out = []
    for v in values:
        try:
            out.append(int(v))
        except ValueError:
            try:
                out.append(float(v))
            except ValueError:
                out.append(v)
    return out


@weather_skill(
    name="concat",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--dim", required=True)
@weather_skill.argument("--coords", help="Comma-separated coord values for the new dim")
def concat(ds, dim, coords, **kwargs):
    """Concatenate Zarr stores along a named dim."""
    import xarray as xr

    # Repeat -i/--input per store. nargs="+" would treat a second -i as a
    # replacement, so only the last input would be written.
    if not isinstance(ds, (list, tuple)) or len(ds) < 2:
        n = 1 if not isinstance(ds, (list, tuple)) else len(ds)
        raise UsageError(f"concat requires at least two --input paths (repeat -i), got {n}")
    dss = list(ds)

    if dim not in dss[0].dims or not all(dim in item.dims for item in dss):
        if coords:
            vals = _coerce([c.strip() for c in coords.split(",")])
            if len(vals) != len(dss):
                raise UsageError(f"--coords len {len(vals)} != inputs {len(dss)}")
            dss = [d.expand_dims({dim: [v]}) for d, v in zip(dss, vals, strict=True)]
        else:
            dss = [d.expand_dims(dim) for d in dss]
    return xr.concat(dss, dim=dim)


if __name__ == "__main__":
    concat()
