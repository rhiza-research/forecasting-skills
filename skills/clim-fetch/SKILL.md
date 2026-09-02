---
name: clim-fetch
description: Fetch a precomputed daily climatology (avg + std) for a `--dataset` (imerg_final, era5, chirps, ...) from Sheerwater's public GCS mirror, select one `--prediction-timedelta` lead, optionally roll it up to a coarser `--window` in days, and expand it onto a requested `--start-time`/`--end-time` calendar window, so timestamps line up with the rest of a pipeline's data. Optional `--bbox N/W/S/E` (compose with resolve-region) subsets before download. Use when a task needs a climatological baseline for anomalies, verification, or comparison — not live observations (use imerg-fetch, dynamical-fetch, arco-era5-fetch, etc. for those).
license: MIT
compatibility: Requires Python 3.12 and uv. Reads a static climatology Zarr from the public GCS bucket sheerwater-public-datalake over anonymous HTTPS; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: fetchers
  variables:
    - precip
---

# clim-fetch

Reads a static day-of-year climatology Zarr mirrored to a public GCS bucket
(`gs://sheerwater-public-datalake/climatologies/<dataset>_<variable>_<window>d.zarr`
— daily is `window=1`, one naming convention for every window). The source
Zarr has dims `init_time`, `prediction_timedelta`, `lat`, `lon` —
1904 init dates (a leap year, so once a lead is selected it always covers
day-of-year 1..366 with no gaps). This skill:

1. Selects one lead via `--prediction-timedelta` (days, default `0`) and
   realizes `time = init_time + prediction_timedelta`.
2. Optionally rolls the climatology up to a coarser `--window` (days) —
   see "Aggregating to a coarser window" below.
3. Re-labels each date in `[--start-time, --end-time]` with its day-of-year
   and gathers the matching climatology row, repeating rows for windows
   spanning more than one year.

One skill covers every mirrored climatology — the source is selected with
`--dataset`.

## When to use

- Need a climatological baseline (mean + spread) to build an anomaly map or
  feed `difference` against live observations or a forecast.
- Want climatology output with real calendar timestamps that match another
  dataset's `time` dim, instead of a bare day-of-year axis.
- Need the climatology at a specific forecast lead (e.g. a 7-day-ahead
  climatological baseline) rather than just the init-date value.

Not for live observations — use `imerg-fetch` / `dynamical-fetch` for IMERG,
`chirps-fetch` for live CHIRPS, `arco-era5-fetch` for ERA5, etc. Not for
datasets not yet mirrored — see "Supported datasets" below.

### Supported datasets

`--dataset` must be the exact bucket product prefix — no aliasing.

| `--dataset` | Source |
| --- | --- |
| `imerg_final` | IMERG final daily precipitation climatology |
| `era5` | ERA5 daily climatology |
| `chirps` | CHIRPS daily precipitation climatology |

More datasets are added by mirroring a new Zarr under the same bucket
convention — no CLI change needed once added.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset <id> --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o <path.zarr> \
  [--variable precip] [--prediction-timedelta 0] [--window 1] [--align left] [--bbox N/W/S/E]
```

### Arguments

- `--dataset` — climatology source id (see table above).
- `--start-time`, `--end-time` — inclusive calendar window, absolute ISO dates
  `YYYY-MM-DD`. The output has one row per calendar day in this window.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--variable`, `-v` — climate variable (default: `precip`); used only as a
  fallback name if the cached Zarr has no `variable` global attr of its own.
- `--prediction-timedelta` — forecast lead in whole days to select from the
  source's `prediction_timedelta` dim (default: `0`). Errors listing the
  available leads if the requested value isn't cached.
- `--window` — climatology window in whole days (default: `1`, i.e. daily).
  See "Aggregating to a coarser window" below.
- `--align` — `left` (default), `right`, or `center`; how a locally-rolled
  `--window` labels each window (matches `aggregate-temporal`'s own
  `--align` default of `left`). Ignored (no-op) on `--window 1` or when a
  pre-aggregated cache is hit.
- `--bbox` — optional `N/W/S/E` bounding box; use `resolve-region` to turn a
  country or named region into this value first. Applied before the
  day-of-year expansion, on the still-lazy remote Zarr, so only the chunks
  overlapping the bbox are pulled from GCS.

### Aggregating to a coarser window

Averaging a `_std` field the same way you average `_avg` computes the wrong
statistic. `--window` handles this correctly so the agent never has to run a
separate aggregation step itself which is prone to errors:

1. Tries a pre-aggregated cache first:
   `climatologies/<dataset>_<variable>_<window>d.zarr`.
2. If that isn't available, falls back to the daily climatology and rolls it
   up locally with a `--window`-day window, labeled per `--align` (e.g.
   `--window 7 --align left` on day-of-year 100 averages days 100–106):
   - `<variable>_avg` rolls like an ordinary rate.
   - `<variable>_std` is computed correctly, assuming independent days:
     `Std(window mean) = sqrt(mean(std_i²)) / sqrt(window)` — squaring,
     rolling through the same mean machinery used for `_avg`, dividing by
     `window`, then taking the square root. This independence assumption
     means the resulting std is an approximation, not exact — a warning to
     that effect prints to stderr whenever this local-rolling fallback is
     used (i.e. whenever a pre-aggregated `--window` cache isn't mirrored).
3. The climatology is circularly padded by `window` days on each end
   before rolling (day 366 wraps to day 1), so days near Jan 1 / Dec 31 get a
   full window instead of coming out `NaN` at the edges.

### Output

Zarr with dims `(time, latitude, longitude)` where `time` is exactly the requested
calendar window (real dates, not the source's 1904 placeholder). Data
variables are named `<variable>_avg` (climatological mean) and
`<variable>_std` (climatological standard deviation), where `<variable>`
comes from the cached Zarr's own `variable` global attr (e.g. `precip_avg`,
`precip_std`) — both converted to standard display units (e.g. `mm day-1`
for precip). Unlike variance, std shares the mean's units and converts
linearly, so both variables go through the same unit-conversion path safely.
Global attrs include `weather_skills_source=sheerwater-mirror:<dataset>`,
`climatology_dataset`, `climatology_variable`,
`climatology_prediction_timedelta_days`, `climatology_window_days`.
Stamped with `data_interval` `1 day` (output is always one row per calendar
day, regardless of `--window`) and, per variable, `aggregation_period`
`"<window> day"` (the width of window each value represents — the two attrs
mean different things: see `aggregate-temporal`'s docs on the same
distinction for its own `--window`).

Two dates far apart within the window but sharing a day-of-year (e.g. two
different years' June 15) get **identical** climatology values by design —
that is the point of the day-of-year gather.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. For this fetcher it
is a length-1 array with `skill="clim-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. Inspect a written output's
provenance with the `provenance` skill.

## Example

```bash
# IMERG climatology for calendar year 2020, at init (lead 0).
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  -o /tmp/imerg_clim_2020.zarr

# Same, but the 7-day-ahead lead's climatology.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  --prediction-timedelta 7 -o /tmp/imerg_clim_2020_7d.zarr

# A 7-day (weekly) smoothed climatology instead of daily.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  --window 7 -o /tmp/imerg_clim_2020_weekly.zarr

# ERA5 climatology spanning two years — June 2020 through June 2021 repeats
# each day-of-year's row across the two Junes.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset era5 --start-time 2020-06-01 --end-time 2021-06-30 \
  -o /tmp/era5_clim.zarr

# Kenya only — resolve-region first, then pass its bbox through.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  --bbox 5.5/33.9/-4.7/41.9 -o /tmp/imerg_clim_2020_kenya.zarr
```

### Recipe: weekly accumulation climatology (3-skill)

`--window` rolls up climatological *rates* (mean and std of the daily rate
averaged over the window) — it does not produce a climatology of *totals*.
For a weekly-accumulation climatology (e.g. "typical weekly CHIRPS rainfall
total"), compose with `select` and `convert-to-totals`: `clim-fetch --window
7` still emits one row per calendar day (overlapping 7-day windows), so
`select` first thins to one row per week before `convert-to-totals` turns
each row's mean/std rate into a total — this is correct for both `_avg` and
`_std` since totaling is linear (`Std(sum) = window × Std(mean)`, the same
relationship `convert-to-totals`'s `rate_to_total` already applies uniformly
to every variable, mean or std alike):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset chirps --start-time 2020-01-01 --end-time 2020-12-31 \
  --window 7 -o /tmp/chirps_clim_weekly_rate.zarr

# Thin to non-overlapping weeks (every 7th day) before totaling.
uv run ${CLAUDE_SKILL_DIR}/../select/scripts/select_dim.py \
  -i /tmp/chirps_clim_weekly_rate.zarr -o /tmp/chirps_clim_weekly_rate_thinned.zarr \
  --dim time --value 2020-01-01 --value 2020-01-08 --value 2020-01-15 # ... etc.

uv run ${CLAUDE_SKILL_DIR}/../convert-to-totals/scripts/convert_to_totals.py \
  -i /tmp/chirps_clim_weekly_rate_thinned.zarr -o /tmp/chirps_clim_weekly_total.zarr
```

The result carries `_avg`/`_std` totals in mm, ready to feed
`standardize-anomaly --climatology` against an observed weekly-accumulated
field (see that skill's own docs for the recipe from the other side).
