---
name: convert-calendar
description: Convert a weather-skills envelope Zarr's time axis to a target CF calendar by wrapping xarray's Dataset.convert_calendar. Use to align two datasets onto a common calendar before comparison — e.g. converting a model-calendar forecast (noleap/360_day) to the standard calendar of observations. Converting to a standard calendar yields a datetime64 axis; converting to a model calendar yields a cftime axis. Dates not representable in the target calendar are dropped.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.5"
  catalog-group: transforms
---

# convert-calendar

Convert the time axis of a weather-skills envelope Zarr to a target CF calendar. CF
datasets may use different calendars — the standard (proleptic Gregorian)
calendar of observations, or a model calendar such as `noleap` (no Feb 29) or
`360_day` (twelve 30-day months). The same wall-clock date maps to a different
day-count per calendar, and some dates don't exist across calendars, so two
datasets on different calendars cannot be compared directly. This skill wraps
xarray's `Dataset.convert_calendar` to put a dataset on a chosen calendar so it
can be aligned with another before comparison. It reads any valid CF input
(including a non-standard model calendar) and emits valid CF.

Converting **to a standard calendar** (`standard`, `proleptic_gregorian`) yields
a `datetime64` time axis. Converting **to a model calendar** (`noleap`,
`360_day`, `all_leap`, `julian`, ...) yields an object-dtype `cftime` axis.

`--align-on` is **required** whenever the source or target calendar is
`360_day`. Dates not representable in the target calendar (e.g. Feb 29 when
converting to `noleap`) are **dropped** from the output.

## When to use

- Aligning a model-calendar forecast (`noleap`/`360_day`) to the standard
  calendar of an observation dataset so the two can be compared.
- Putting two datasets on a common CF calendar before any calendar-sensitive
  step.

Not for: resampling or rolling up the time axis into fixed windows (daily,
weekly, monthly) — that is `aggregate-temporal`. Not for comparing or plotting
two datasets — that is `plot-compare`. This skill changes only the calendar of
the time axis; it does not resample, reduce, or render.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py --input <in.zarr> --output <out.zarr> \
    --calendar NAME \
    [--time-dim NAME] [--align-on date|year]
```

### Arguments
- `--input`, `-i` — input Zarr (any envelope with a wall-clock `time` axis).
- `--output`, `-o` — output Zarr.
- `--calendar` — target CF calendar name (`standard`, `proleptic_gregorian`,
  `noleap`, `360_day`, `all_leap`, `julian`, ...).
- `--time-dim` — name of the time dim when it is not auto-detectable via CF
  metadata. Defaults to the CF "T" axis (cf-xarray).
- `--align-on` — `date` or `year`. **Required** when the source or target
  calendar is `360_day`. `year` translates dates by relative position in the
  year (best for daily/sub-daily data); `date` conserves month/day and drops
  invalid dates (best for coarser-than-daily data). Omitted for conversions not
  involving `360_day`.

### Output

Same variables, dims, and coords as the input, with the time axis re-expressed
on the target calendar. Converting to a standard calendar gives a
`datetime64` axis; converting to a model calendar gives a `cftime` axis. Dates
not representable in the target calendar are dropped (so the time axis may be
shorter than the input's). CF coordinate and data-variable attributes are
preserved.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. This skill reads the upstream
input's `weather_skills_history` (default `[]` and a stderr warning if absent) and
appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings (so `calendar`, `time_dim`, `align_on`);
`input` is a `{basename, hash}` dict — `basename` is the upstream zarr's
filename and `hash` is a sha256 of its stored bytes; `version` is the
`_SKILL_VERSION` constant in `scripts/convert_calendar.py`, kept in
lockstep with `metadata.version` in this SKILL.md by the CI version-bump
workflow. Cache-hit comparison reads the existing output's `weather_skills_history`: a
hit requires the upstream chain to match and the last entry's `skill`,
`version`, `args`, and `input.basename` to match the proposed new entry; on a
hit the script returns without recomputing. The `input.hash` is not part of the
cache key, so a same-named input whose content changed in place still hits on
basename.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`align_on`), not the hyphenated CLI flag names (`--time-dim`, `--align-on`). A
consumer reconstructing a `uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>`
invocation must translate underscore → hyphen.

## Examples

```bash
# Convert a noleap forecast to the standard calendar so it can be compared
# against standard-calendar observations.
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py \
    -i /tmp/forecast_noleap.zarr -o /tmp/forecast_standard.zarr \
    --calendar standard
```

```bash
# Convert standard-calendar observations onto a 360_day model calendar.
# --align-on is required because 360_day is involved.
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_calendar.py \
    -i /tmp/obs.zarr -o /tmp/obs_360.zarr \
    --calendar 360_day --align-on year
```
