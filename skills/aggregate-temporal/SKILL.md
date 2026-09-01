---
name: aggregate-temporal
description: Roll up a weather-skills standard dataset Zarr along its time axis (or forecast step axis) into fixed windows (daily, weekly, dekadal, monthly, or a pint duration like '21 day') or a rolling --window, with mean/min/max. Keeps data_interval; stamps aggregation_period, aggregation_coverage, and CF cell_methods. Rates in and rates out — use convert-to-totals for period amounts (refuses overlapping rolling series; select non-overlapping times first). `--climatology NAME` correctly aggregates a `NAME_avg`/`NAME_std` pair (e.g. from clim-fetch) — averaging <name>_std with a plain --method mean silently computes the wrong statistic.
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
- For period **totals** (`mm`), run `convert-to-totals` afterward (non-overlapping bins only; rolling series with Δt &lt; `aggregation_period` are refused — `select` the times you want first). A single remaining bin is allowed. Incomplete bins are kept and stamped with `aggregation_coverage` &lt; 1; convert-to-totals `--min-coverage` (default 1.0) drops them.
- Rolling up a climatology's `NAME_avg`/`NAME_std` pair (e.g. from `clim-fetch`) to a coarser window — pass `--climatology NAME` instead of `--variable`. See "Aggregating a climatology pair" below; do **not** aggregate a `_std`/`_variance` field with a plain `--method mean` — it silently computes the wrong number (see below).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --period daily|weekly|dekadal|monthly|'21 day' [--method mean|max|min] \
    [--variable VAR ...] [--time-dim DIM] \
    [--start-time YYYY-MM-DD] [--end-time YYYY-MM-DD]

uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --window N [--align left|right|center] [--stride STEP] \
    [--method mean|max|min] [--variable VAR ...] [--time-dim DIM]

uv run ${CLAUDE_SKILL_DIR}/scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --climatology NAME (--period ... | --window N)
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
  Refused when the input has CF `{dim}_bounds` (use `--period`).
- `--align` — with `--window`: label placement `left` (default), `right`, or `center`.
- `--stride` — with `--window`: integer subsample step, or a date stride (`day`, `week`, `month`, `year`, or weekday names like `Monday`).
- `--method` — reducer: `mean` (default), `max`, `min`. There is no `sum`; totals are a separate skill.
- `--variable`, `-v` — repeatable; restricts aggregation to the named data variable(s). Mutex with `--climatology`.
- `--climatology` — semantic name whose `NAME_avg`/`NAME_std` pair gets correct, statistics-aware aggregation instead of `--variable`. See "Aggregating a climatology pair" below. Requires `--method mean` (the default); mutex with `--variable`.
- `--time-dim` — override; by default uses `time` if present, else `step`.
- `--end-time` — with `--period` on a `time` axis: date the final bin ends on (bins walk backward). Same standard flag as fetchers. No effect on `step` or `--window`.
- `--start-time` — with `--period` and `--end-time`: optional earliest coverage floor. Requires `--end-time`.

### Aggregating a climatology pair

Averaging a `_std` (or `_variance`) field the same way you average a mean
field gives the wrong number. Mean is linear — a weighted average of daily
means equals the mean you'd get from the raw samples directly. Variance/std
is **quadratic** in the underlying samples (`Var(Σ wᵢXᵢ) = Σ wᵢ² Var(Xᵢ)`), so
no generic reducer (`mean`/`max`/`min`) ever computes the variance/std of an
aggregated mean correctly — it needs its own formula, not a bigger `--method`
list.

`--climatology NAME` aggregates `NAME_avg` normally, and — assuming
independent days, equal weights — computes `NAME_std` correctly as the
standard error of the aggregated mean:

```
Var(window mean) = (1/N²) · Σ stdᵢ²
Std(window mean) = sqrt(Σ stdᵢ²) / N
```

Implementation: square `NAME_std`, run it through the exact same
mean-aggregation used for `NAME_avg` (giving `mean(stdᵢ²) = Σstdᵢ²/N`),
divide by `N` once more, then take the square root. `N` is the rolling
`--window` size, or the nominal sample count for the resolved `--period` bin
(e.g. days-in-month for `monthly`) — not the coverage-adjusted actual count.

Constraints:
- Requires both `NAME_avg` and `NAME_std` present in the input; errors listing
  what's missing otherwise.
- Requires `--method mean` — variance/std under max/min isn't a defined thing.
- Mutex with `--variable` — a `--climatology` call aggregates only that one
  pair, nothing else, in one call.
- Not supported on CF-bounds (duration-weighted) input — errors rather than
  silently assuming equal weights where the input says otherwise. `clim-fetch`
  output never has CF bounds, so this only matters for hand-built inputs.
- Works with both `--period` and `--window`.

### Metadata stamped

On each aggregated data variable:

- `data_interval` — native sample spacing from the fetch (kept when the input was uniform; omitted when the input had CF bounds).
- `aggregation_period` — pint duration for the window (`1 day`, `7 day`,
  `1 dekad`, `1 month`, `21 day`, or e.g. `7 day` for `--window 7` on daily
  data) used later by `convert-to-totals`.
- `cell_methods` — CF statistic, e.g. `time: mean (interval: 1 day)`.
  `interval:` is the **input** sample spacing when it is a scalar `data_interval`.
  With `--climatology`, `NAME_std` is still stamped `mean` — CF has no
  standard cell method for "standard error of the mean"; treat this stamp as
  informational for `NAME_std`, not literally accurate.

On the time/step axis:

- `aggregation_coverage` — completeness of each interval vs native cells
  (0–1). Incomplete bins are kept. Uniform: expected count =
  `aggregation_period / data_interval` (or vs the input `aggregation_period`
  on a re-aggregate). Irregular CF bounds: covered duration / window.
  convert-to-totals `--min-coverage` (default 1.0) drops incomplete bins.

Inputs with CF `{dim}_bounds` are duration-weighted (`sum(rate × dt) / sum(dt)`), so a 1-day cell and a 5-day cell in the same week are not equal-weighted. `--window` is a step count and is refused on those axes.

Rate `units` are unchanged (still `mm day-1`, etc.).

### Output

Same variables; the time/step axis is replaced by the aggregated window.

### Provenance

History entry records period/window, method, variables, and paths as usual.
