# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cftime",
#   "numpy",
# ]
# ///
"""Convert a weather-skills envelope Zarr's time axis to a target CF calendar.

Wraps xarray's ``Dataset.convert_calendar`` so two datasets on different CF
calendars can be aligned to a common calendar before comparison. Converting to a
standard calendar yields a ``datetime64`` time axis; converting to a model
calendar (``noleap``, ``360_day``, ...) yields object-dtype ``cftime``. Dates not
representable in the target calendar (e.g. Feb 29 when converting to ``noleap``)
are dropped. ``--align-on`` is required whenever the source or target calendar is
``360_day``.
"""

import sys

from weather_skills_core import UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.8"

def _source_calendar(time_coord) -> str:
    """Best-effort name of the source calendar of a decoded time coordinate.

    A datetime64 axis is the proleptic Gregorian (``standard``) calendar; an
    object-dtype cftime axis carries its calendar on each element. Returns
    ``"standard"`` when the calendar cannot be read off the values.
    """
    import numpy as np

    vals = np.asarray(time_coord.values)
    if vals.dtype.kind == "M":
        return "standard"
    if vals.size and hasattr(vals.flat[0], "calendar"):
        return vals.flat[0].calendar
    return "standard"

@weather_skill(
    name="convert-calendar",
    version=_SKILL_VERSION,
    inputs=["any"],
    outputs=["any"]
)
@weather_skill.argument(
            "--calendar",
            required=True,
            choices=[
                "standard",
                "gregorian",
                "proleptic_gregorian",
                "noleap",
                "365_day",
                "360_day",
                "all_leap",
                "366_day",
                "julian",
            ],
            help="Target CF calendar name (e.g. standard, proleptic_gregorian, "
            "noleap, 360_day, all_leap, julian).",
        )
@weather_skill.argument(
            "--align-on",
            choices=["date", "year"],
            help="How to map dates across calendars. Required whenever the source "
            "or target calendar is 360_day. 'year' translates dates by relative "
            "position in the year (best for daily/sub-daily); 'date' conserves "
            "month/day and drops invalid dates (best for coarser-than-daily).",
        )
@weather_skill.argument(
            "--time-dim",
            help="Name of the time dim when it is not auto-detectable via CF metadata.",
        )
def convert_calendar(ds, calendar, align_on, time_dim, **kwargs):
    """Convert a weather-skills envelope Zarr's time axis to a target CF calendar."""
    import numpy as np
    from weather_skills_core.dataset import detect_time_dim

    # Identify the wall-clock time dim. Honor an explicit --time-dim override,
    # else use cf-xarray's CF "T" axis detection (finds time even when named
    # unusually). Calendar conversion only applies to a wall-clock time axis,
    # not a forecast `step` (timedelta64) lead-time axis.
    time_dim = detect_time_dim(ds, time_dim)

    # Validate that the resolved dim is actually a wall-clock time axis before
    # touching the calendar machinery. A datetime64 axis (dtype kind "M") or an
    # object-dtype cftime axis (first element carries a `.calendar` attr) is a
    # time axis; a spatial/coordinate dim pointed at by --time-dim is not. An
    # empty axis is also rejected here: it would otherwise let _source_calendar
    # fall back to "standard" and silently bypass the 360_day align-on guard.
    time_vals = np.asarray(ds[time_dim].values)
    if time_vals.size == 0:
        raise UsageError(f"time dim '{time_dim}' is empty; nothing to convert.")
    is_datetime64 = time_vals.dtype.kind == "M"
    is_cftime = time_vals.dtype.kind == "O" and hasattr(time_vals.flat[0], "calendar")
    if not (is_datetime64 or is_cftime):
        raise UsageError(
            f"dim '{time_dim}' is not a time axis "
            f"(dtype {time_vals.dtype}); expected datetime64 or cftime values. "
            "Pass --time-dim pointing at the wall-clock time dim."
        )

    # xarray requires align_on when 360_day is on either side of the conversion
    # (it cannot otherwise map between a 360-day year and a calendar with months
    # of varying length). Guard up front with a message naming the flag.
    source_calendar = _source_calendar(ds[time_dim])
    if (source_calendar == "360_day" or calendar == "360_day") and align_on is None:
        raise UsageError(
            "--align-on is required when the source or target calendar "
            f"is 360_day (source={source_calendar!r}, target={calendar!r}). "
            "Pass --align-on date or --align-on year."
        )

    print(
        f"Converting dim={time_dim} calendar {source_calendar!r} -> "
        f"{calendar!r} (align_on={align_on!r})",
        file=sys.stderr,
    )
    out_ds = ds.convert_calendar(calendar, dim=time_dim, align_on=align_on)

    # If every source timestep is unrepresentable in the target calendar (e.g.
    # converting a series that is entirely Feb 29 / Feb 30 dates), xarray drops
    # them all and leaves a zero-length time axis. Refuse to write an empty store.
    n_in = ds.sizes[time_dim]
    if out_ds.sizes.get(time_dim, 0) == 0:
        raise UsageError(
            f"conversion to calendar {calendar!r} dropped all "
            f"{n_in} timesteps (none representable in the target calendar); "
            "nothing to write."
        )

    return out_ds

if __name__ == "__main__":
    convert_calendar()
