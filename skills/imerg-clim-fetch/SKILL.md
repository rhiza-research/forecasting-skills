---
name: imerg-clim-fetch
description: Fetch IMERG daily precipitation climatology (1998–2023 day-of-year mean from IMERG final) for a calendar window via Sheerwater's cached public archive and write a weather-skills standard dataset Zarr. Use when a task needs a gridded IMERG baseline for anomalies, verification, or comparison — not live IMERG observations (use imerg-fetch or dynamical-fetch for those).
license: MIT
compatibility: Requires Python 3.12 and uv. Reads precomputed climatology from Sheerwater's public GCS cache via the sheerwater package; no Earthdata credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: fetchers
  variables:
    - precip
---

# imerg-clim-fetch

Loads IMERG final daily precipitation climatology through Sheerwater's
`climatology_imerg_1998_2024` accessor. The baseline is the day-of-year mean over
1998–2023 on the requested Sheerwater grid; values for `--start-time` through
`--end-time` are the climatological mean for each calendar day in that window
(not live satellite retrievals).

Sheerwater caches computed climatologies in a public bucket, so reads are
credential-free.

## When to use

- Need an IMERG precipitation climatology baseline for anomaly maps or
  `difference` against observations or forecasts.
- Want IMERG climatology on a standard Sheerwater grid (`global0_25` or
  `global1_5`) without building the baseline yourself.

Not for live IMERG rainfall — use `imerg-fetch` or `dynamical-fetch`
`nasa-imerg-analysis-*`. Not for CHIRPS or ERA5 climatology (other Sheerwater
climatology accessors exist in the library but are not exposed by this skill).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --start-time YYYY-MM-DD --end-time YYYY-MM-DD --output <path.zarr> \
  [--grid global0_25|global1_5] [--region <sheerwater-region>]
```

### Arguments

- `--start-time`, `--end-time` — inclusive calendar window. Each value is an
  absolute ISO date `YYYY-MM-DD`. The skill returns the climatological mean for
  each day-of-year in that window.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--variable`, `-v` — data variable to fetch (only `precip` is supported).
- `--grid` — Sheerwater grid id: `global0_25` (0.25°, default) or `global1_5`
  (1.5°).
- `--region` — Sheerwater spatial region (default: `global`). Passed through to
  the climatology accessor; use a sub-global region to reduce memory use.

### Output

Zarr with data variable `precip` (mm/day rate) and dims `(time, lat, lon)` on the
chosen Sheerwater grid. Global attrs include
`weather_skills_source=sheerwater:climatology_imerg_1998_2024`,
`climatology_first_year=1998`, `climatology_last_year=2023`, and
`climatology_data_source=imerg_final`. Stamped with `data_interval` `1 day`.

### Memory and performance

Global `global0_25` is large (~1440×721 cells per day). The skill materializes
the requested window in memory before writing. Pass `--region` (e.g. `Africa`) to
clip during the Sheerwater read, keep the date window focused, or follow with
`clip-region` to shrink further.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only
array of per-step entries `{skill, version, args, input}`. For this fetcher it is
a length-1 array with `skill="imerg-clim-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records the run's flag values
under underscored names; `version` is the value printed by `--help`. Inspect a
written output's provenance with the `provenance` skill.

## Example

```bash
# 2020 calendar year on the 0.25° Sheerwater grid.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --start-time 2020-01-01 --end-time 2020-12-31 \
  --grid global0_25 --output /tmp/imerg_clim_2020.zarr

# Same window on the coarser 1.5° grid.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --start-time 2020-01-01 --end-time 2020-12-31 \
  --grid global1_5 --output /tmp/imerg_clim_2020_1p5.zarr

# Africa only — smaller than global on the 0.25° grid.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --start-time 2020-01-01 --end-time 2020-12-31 \
  --grid global0_25 --region Africa --output /tmp/imerg_clim_2020_africa.zarr
```
