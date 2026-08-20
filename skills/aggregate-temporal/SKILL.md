---
name: aggregate-temporal
description: Roll up a weather-skills standard dataset Zarr along its time axis (or forecast step axis) into fixed windows (daily, weekly, dekadal, monthly, or a pint duration like '21 day') or a rolling --window, with mean/min/max. Keeps data_interval; stamps aggregation_period, aggregation_coverage, and CF cell_methods. Rates in and rates out — use convert-to-totals for period amounts (refuses overlapping rolling series; select non-overlapping times first).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py *)
metadata:
  catalog-group: transforms
---

# aggregate-temporal

Source-agnostic temporal aggregation of **rates**. Works on:
- Observation datasets with a `time` dim (e.g. CHIRPS, IMERG, TAHMO).
- Forecast datasets with a `step` dim (e.g. ECMWF S2S).

Autodetects which dim is present. For forecasts, aggregates ensemble members (`number`) independently.

## When to use

- Turning daily or half-hourly rates into weekly/dekadal/monthly/custom-duration
  **mean** (or min/max) rates (`--period weekly` or `--period '21 day'`).
- Rolling N-step means (`--window`) with optional `--align` / `--stride`.
- Selecting weekly or dekadal subsets of a forecast initialized at multiple steps.
- For period **totals** (`mm`), run `convert-to-totals` afterward (non-overlapping bins only; rolling series with Δt &lt; `aggregation_period` are refused — `select` the times you want first). A single remaining bin is allowed. Incomplete bins dropped by default; pass `--keep-partial` so `aggregation_coverage` &lt; 1 is available for `--min-coverage`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --period daily|weekly|dekadal|monthly|'21 day' [--method mean|max|min] \
    [--variable VAR ...] [--time-dim DIM] \
    [--start-time YYYY-MM-DD] [--end-time YYYY-MM-DD] [--keep-partial]

uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --window N [--align left|right|center] [--stride STEP] \
    [--method mean|max|min] [--variable VAR ...] [--time-dim DIM]
```

Exactly one of `--period` or `--window` is required.

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr.
- `--period` — calendar/step window: `daily` (1d), `weekly` (7d), `dekadal` (10d),
  `monthly` (calendar month for the default forward resample; 30-day fixed width
  with `--end-time`), or a pint duration in whole days (`21 day`, `3 week`).
  Mutex with `--window`. Use a duration when you need a window the named
  periods do not cover (e.g. 21-day totals from half-hourly IMERG).
- `--window` — rolling window length in axis steps. Mutex with `--period`.
- `--align` — with `--window`: label placement `left` (default), `right`, or `center`.
- `--stride` — with `--window`: integer subsample step, or a date stride (`day`, `week`, `month`, `year`, or weekday names like `Monday`).
- `--method` — reducer: `mean` (default), `max`, `min`. There is no `sum`; totals are a separate skill.
- `--variable`, `-v` — repeatable; restricts aggregation to the named data variable(s).
- `--time-dim` — override; by default uses `time` if present, else `step`.
- `--end-time` — with `--period` on a `time` axis: date the final bin ends on (bins walk backward). Same standard flag as fetchers. No effect on `step` or `--window`.
- `--start-time` — with `--period` and `--end-time`: optional earliest coverage floor. Requires `--end-time`.
- `--keep-partial` — with `--period` (forward resample and `--end-time`): keep incomplete bins (e.g. a trailing week with fewer than 7 daily samples) and stamp `aggregation_coverage` &lt; 1. **Default drops them**. Convert-to-totals then applies `--min-coverage` (default 1.0). No effect on `--window` or `step` (those paths already omit incomplete windows).

### Metadata stamped

On each aggregated data variable:

- `data_interval` — native sample spacing from the fetch (kept; not the window).
- `aggregation_period` — pint duration for the window (`1 day`, `7 day`,
  `1 dekad`, `1 month`, `21 day`, or e.g. `7 day` for `--window 7` on daily
  data) used later by `convert-to-totals`.
- `cell_methods` — CF statistic, e.g. `time: mean (interval: 1 day)`.
  `interval:` is the **input** sample spacing when it can be inferred.

On the time/step axis:

- `aggregation_coverage` — completeness of each interval vs `data_interval`
  (0–1). Expected count = `aggregation_period / data_interval`.

Rate `units` are unchanged (still `mm day-1`, etc.).

### Output

Same variables; the time/step axis is replaced by the aggregated window.

### Provenance

History entry records period/window, method, variables, and paths as usual.
