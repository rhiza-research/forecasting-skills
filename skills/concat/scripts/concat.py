# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
#   "xarray",
# ]
# ///
"""Concatenate Zarr stores along a named dim."""

from weather_skills_core import Types, UsageError, weather_skill

_SKILL_VERSION = "0.1.10"


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
    inputs=[Types.ANY + "+2"],
    outputs=[Types.ANY],
)
@weather_skill.argument("--dim", required=True, help="Dimension to concatenate along.")
@weather_skill.argument(
    "--coords",
    action="append",
    help="Coord value for the new dim (repeat once per input).",
)
def concat(dss, dim, coords):
    """Concatenate Zarr stores along a named dim."""
    import xarray as xr

    if dim not in dss[0].dims or not all(dim in ds.dims for ds in dss):
        if coords:
            vals = _coerce(coords)
            if len(vals) != len(dss):
                raise UsageError(f"--coords len {len(vals)} != inputs {len(dss)}")
            dss = [d.expand_dims({dim: [v]}) for d, v in zip(dss, vals, strict=True)]
        else:
            dss = [d.expand_dims(dim) for d in dss]
    return xr.concat(dss, dim=dim)


if __name__ == "__main__":
    concat()
