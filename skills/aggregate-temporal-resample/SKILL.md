---
name: aggregate-temporal-resample
description: Resample a Rhiza Envelope Zarr along its time axis (or forecast step axis) into NON-OVERLAPPING calendar-aligned windows (daily, weekly, dekadal, monthly) with a chosen reducer. Use specifically when you need calendar months or non-overlapping period totals — sheerwater's `aggregate-temporal` only does ROLLING windows. Otherwise prefer the sheerwater skill.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  openclaw:
    requires:
      bins:
        - uv
---

# aggregate-temporal-resample

Source-agnostic temporal aggregation using `xarray.resample` (non-overlapping, calendar-aligned). Works on:
- Observation envelopes with a `time` dim (e.g. CHIRPS, IMERG, TAHMO).
- Forecast envelopes with a `step` dim (e.g. ECMWF S2S).

Autodetects which dim is present. For forecasts, aggregates ensemble members (`number`) independently.

## How this differs from sheerwater's `aggregate-temporal`

Sheerwater also has an `aggregate-temporal` skill, but it's backed by `roll_and_agg` which does **rolling** windows only — every output point is the mean/sum over the previous N days, sliding by one day. That's the right model for "smooth my daily series with a 7-day filter".

This skill uses `xarray.resample` which does **non-overlapping calendar-aligned** windows — the year is partitioned into N-day blocks (or calendar months) and each gets one aggregated value. That's the right model for "give me a single value per dekad / per month".

If both skills are installed, route by the operation you actually need:

- "Rolling 7-day mean of daily precip" → sheerwater `aggregate-temporal`.
- "One value per dekad / per calendar month / per ISO week" → this skill.
- "Calendar months specifically (not 30-day rolling)" → only this skill can do it; sheerwater's rolling form can't express calendar months.

When sheerwater adds a calendar-aligned resample sibling (see TODO in `sheerwater.utils.data_utils.roll_and_agg`), this skill becomes redundant and will be removed.

## When to use

- Turning daily/half-hourly observations into weekly, dekadal, or monthly totals.
- Selecting weekly or dekadal non-overlapping subsets of a forecast.

## Usage

```
uv run scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --period daily|weekly|dekadal|monthly [--method sum|mean|max|min] \
    [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr.
- `--period` — window size: `daily` (1d), `weekly` (7d), `dekadal` (10d), `monthly` (calendar month).
- `--method` — reducer: `sum` (default for totals), `mean`, `max`, `min`.
- `--time-dim` — override; by default uses `time` if present, else `step`.

### Output

Same variables; the time/step axis is replaced by the aggregated window. `rhiza_aggregation` attr is stamped (e.g. `weekly-sum`).

## Examples

```bash
uv run scripts/aggregate.py -i /tmp/imerg.zarr -o /tmp/imerg_dekadal.zarr \
    --period dekadal --method sum
```

```bash
uv run scripts/aggregate.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_weekly.zarr \
    --period weekly --method sum
```
