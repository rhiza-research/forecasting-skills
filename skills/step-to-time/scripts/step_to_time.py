# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime>=1.6",
#   "numpy>=2.4",
# ]
# ///
"""Realize forecast step as wall-clock time (time = init + step)."""

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(
    name="step-to-time",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("forecast"), required=True)
def step_to_time(ds, **kwargs):
    """Realize forecast step as wall-clock time (time = init + step)."""
    import cftime
    import numpy as np

    if "step" not in ds.dims or "time" in ds.dims:
        raise UsageError("need forecast shape: scalar time coord + step dim")
    init = ds["time"]
    step = ds["step"]
    init_scalar = init.values.item() if hasattr(init.values, "item") else init.values
    if isinstance(init_scalar, cftime.datetime):
        valid = np.array(
            [init_scalar + td.item() for td in step.values.astype("timedelta64[us]")],
            dtype=object,
        )
        init_iso = init_scalar.isoformat()
    else:
        valid = (init.values + step.values).astype("datetime64[ns]")
        init_iso = str(np.datetime_as_string(init.values.astype("datetime64[s]")))
    drop = ["time"] + (["valid_time"] if "valid_time" in ds.variables else [])
    bound_name = ds["step"].attrs.get("bounds") if "step" in ds.coords else None
    if not (isinstance(bound_name, str) and bound_name in ds) and "step_bounds" in ds:
        bound_name = "step_bounds"
    out = ds.drop_vars(drop).rename({"step": "time"}).assign_coords(time=("time", valid))
    out["time"].attrs.setdefault("standard_name", "time")
    out["time"].attrs.setdefault("axis", "T")
    if isinstance(bound_name, str) and bound_name in out:
        pairs = np.asarray(out[bound_name].values)
        if isinstance(init_scalar, cftime.datetime):
            time_pairs = np.empty(pairs.shape, dtype=object)
            for i in range(pairs.shape[0]):
                for j in range(2):
                    time_pairs[i, j] = (
                        init_scalar + np.asarray(pairs[i, j]).astype("timedelta64[us]").item()
                    )
        else:
            time_pairs = (init.values + pairs).astype("datetime64[ns]")
        out = out.drop_vars(bound_name).assign_coords(time_bounds=(("time", "nv"), time_pairs))
        out["time"].attrs["bounds"] = "time_bounds"
    out.attrs["weather_skills_forecast_init"] = init_iso
    return out


if __name__ == "__main__":
    step_to_time()
