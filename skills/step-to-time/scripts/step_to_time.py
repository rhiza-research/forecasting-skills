# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "numpy>=2.4",
# ]
# ///
"""Realize a forecast's step axis as wall-clock valid times (time = init + step)."""

from weather_skills_core import Types, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.8"


@weather_skill(
    name="step-to-time",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.GRIDDED],
)
def step_to_time(ds):
    """Realize a forecast's step axis as wall-clock valid times (time = init + step)."""
    import cftime
    import numpy as np

    if "time" in ds.dims and "step" in ds.dims:
        raise UsageError(
            "input has both a 'time' dimension and a 'step' dimension "
            "(a multi-init/hindcast cube); per-init step realization is not "
            "supported. Select a single init first."
        )
    if "time" in ds.dims:
        raise UsageError(
            "input already has a wall-clock time axis "
            "('time' is a dim, not a scalar forecast-init coord); nothing to realize."
        )
    if "step" not in ds.dims:
        raise UsageError(f"input has no 'step' dim; got dims {list(ds.dims)}.")
    if ds.sizes["step"] == 0:
        raise UsageError("'step' dim has length 0; nothing to realize.")
    step = ds["step"]
    if not np.issubdtype(step.dtype, np.timedelta64):
        raise UsageError(
            f"'step' coord is not a lead time (dtype {step.dtype}, "
            "expected timedelta64); cannot realize valid times from it."
        )
    if np.isnat(step.values).any():
        raise UsageError("step contains NaT (not-a-time) values; cannot realize valid times.")
    if "time" not in ds.coords or ds["time"].ndim != 0:
        raise UsageError(
            "input has no scalar 'time' coord holding the forecast init date; "
            f"got coords {list(ds.coords)}."
        )
    init_coord = ds["time"]
    init = init_coord.values

    is_datetime64 = np.issubdtype(init_coord.dtype, np.datetime64)
    init_scalar = init.item() if hasattr(init, "item") else init
    is_cftime = isinstance(init_scalar, cftime.datetime)
    if not (is_datetime64 or is_cftime):
        raise UsageError(
            f"scalar 'time' coord is not a datetime64 or cftime init date "
            f"(dtype {init_coord.dtype}, value type {type(init_scalar).__name__})."
        )

    if is_datetime64:
        if np.isnat(init):
            raise UsageError("init date is missing/NaT.")
    else:
        if init_scalar is None or init_scalar != init_scalar:  # noqa: PLR0124
            raise UsageError("init date is missing/NaT.")

    if is_datetime64:
        valid_times = (init + step.values).astype("datetime64[ns]")
        init_iso = str(np.datetime_as_string(init.astype("datetime64[s]")))
    else:
        steps_td = step.values.astype("timedelta64[us]")
        valid_times = np.array(
            [init_scalar + td.item() for td in steps_td],
            dtype=object,
        )
        init_iso = init_scalar.isoformat()

    if len(valid_times) > 1 and not all(
        valid_times[i] < valid_times[i + 1] for i in range(len(valid_times) - 1)
    ):
        raise UsageError(
            "realized valid times are not strictly increasing "
            "(duplicate or out-of-order); cannot build a monotonic time axis."
        )

    drop = ["time"]
    if "valid_time" in ds.variables:
        drop.append("valid_time")
    out_ds = ds.drop_vars(drop).rename({"step": "time"})
    out_ds = out_ds.assign_coords(time=("time", valid_times))
    out_ds["time"].attrs.setdefault("standard_name", "time")
    out_ds["time"].attrs.setdefault("axis", "T")
    out_ds.attrs["weather_skills_forecast_init"] = init_iso
    return out_ds


if __name__ == "__main__":
    step_to_time()
