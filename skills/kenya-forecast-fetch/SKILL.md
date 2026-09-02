---
name: kenya-forecast-fetch
description: Fetch a forecast grid from the public Kenya forecasts archive (gs://kenya-forecasting-data/<date>/data/). Native ECMWF S2S is the low-resolution Zarr (`--dataset precip`, ~1.5°); the CHIRPS-resolution weekly downscale is `--dataset precip_downscaled` (~0.05°, data_weekly_Kenya_downscaled.nc). Also GEFS, medium-range precip, temps, and winds. Use for clipping, aggregation, comparison, or plotting via plot / plot-timeseries / plot-mediogram. For pre-rendered product PNGs, use kenya-forecast-png instead.
license: MIT
compatibility: Requires Python 3.12 and uv. Opens public consolidated Zarr (native S2S / GEFS / medium-range) or the weekly downscaled NetCDF over HTTPS from Google Cloud Storage bucket kenya-forecasting-data; no credentials required. Older init folders may only have GRIB/NetCDF under data/ and no Zarr.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - d2m
    - cape
    - tcw
---

# kenya-forecast-fetch

Opens a store under the KMSA / Rhiza Kenya forecasts archive
`data/` folder, subsets it, maps it onto the weather-skills standard dataset,
and writes a local Zarr. The archive is browsable at
https://kenya-forecasts.sheerwater.rhizaresearch.org/files/; this skill reads
the same objects from `gs://kenya-forecasting-data` over HTTPS.

Layout:

```
YYYY-MM-DD/data/ECMWF_s2s_precip_YYYY-MM-DD.zarr/     # native S2S ~1.5°
YYYY-MM-DD/data/data_weekly_Kenya_downscaled.nc       # CHIRPS-grid weekly ~0.05°
YYYY-MM-DD/data/medium_range_precip.zarr/
YYYY-MM-DD/data/gefs/gefs_kenya.zarr/
…
```

By default the skill takes the **most recent** init date that has the requested
`--dataset`. Pass `--date` to pin an init.

This skill does **not** plot. For flexible figures, chain to `plot`,
`plot-timeseries`, `plot-mediogram`, `summarize-dim`, `clip-region`, etc. For the
archive's pre-rendered product PNGs, use `kenya-forecast-png`.

## When to use

- Need Kenya-region ensemble / medium-range grids already published in the
  pilot archive (no ECDS queue).
- Native S2S precip (`--dataset precip`) vs the statistically downscaled
  weekly precip (`--dataset precip_downscaled`) for the same init.

Prefer `dynamical-fetch` when you need a live global GEFS / IFS / GFS fetch.
Use this skill for Kenya-region grids already published in the pilot archive
(no ECDS queue). Prefer `ecmwf-fetch` only for S2S, or when an init date's
`data/` folder only has legacy GRIB/NetCDF (no `.zarr` / downscaled weekly file).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> \
    [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [dataset-id]
```

### Datasets

| `--dataset` | Store under `<date>/data/` | Typical vars |
|---|---|---|
| `precip` (default) | `ECMWF_s2s_precip_<date>.zarr` | `tp` — native S2S, ~1.5°, daily ensemble |
| `precip_downscaled` | `data_weekly_Kenya_downscaled.nc` | `tp` — CHIRPS-grid weekly totals, ~0.05°, no `number` |
| `daily_vars` | `ECMWF_s2s_daily_vars_<date>.zarr` | `t2m`, `d2m`, `cape`, `tcw` |
| `Tminmax` | `ECMWF_s2s_Tminmax_<date>.zarr` | `mn2t6`, `mx2t6` |
| `10wind` | `ECMWF_s2s_10wind_<date>.zarr` | `u10`, `v10` |
| `500wind` | `ECMWF_s2s_500wind_<date>.zarr` | `w` (and related) |
| `700wind` | `ECMWF_s2s_700wind_<date>.zarr` | `u`, `v` |
| `medium_range_precip` | `medium_range_precip.zarr` | `tp` |
| `gefs` | `gefs/gefs_kenya.zarr` | `tp` |

Output dims follow the classic forecast shape: scalar `time` (init) + `step`
(+ `number` for ensembles) + `latitude`/`longitude`. Fetch writes precipitation
as a per-step **rate** (`mm day-1`) and known air temperature as
`degree_Celsius`. Interval fields (`precip`, `precip_downscaled`, `gefs`,
`daily_vars`, `medium_range_precip`) are **left-labeled**: `step = 0` is the
first native period (`[init, init+1d)` for daily precip; `[init, init+7d)`
for weekly downscaled / medium-range). Instantaneous winds / Tminmax keep
archive `step` (lead 0 = 00Z analysis). Daily products (`precip`, `gefs`,
`daily_vars`): `aggregate-temporal --period weekly` then `convert-to-totals`.
Already-weekly products (`precip_downscaled`, `medium_range_precip`) stamp
`aggregation_period` on fetch — run `convert-to-totals` directly; do **not**
`aggregate-temporal` (that would re-bin adjacent weeks). Do not run
`deaccumulate` after this skill. The downscaled file is already weekly on a
~0.05° grid — do not also run `downscale`.

### Arguments

- `--dataset` — product id from the table (default `precip`, the native
  low-resolution S2S precip). High-resolution weekly downscale:
  `precip_downscaled`.
- `--date` — optional init date `YYYY-MM-DD`. Default: latest folder with that
  Zarr. Calendar day: `resolve-time latest`. Latest published init: `--probe-latest`.
- `--probe-latest [dataset-id]` — print the latest init `YYYY-MM-DD` on stdout and exit. No `-o`.
- `--bbox` — optional spatial subset `N/W/S/E`.
- `--variable`, `-v` — restrict to named data variables (repeatable).
- `--output`, `-o` — output Zarr path.

### Example: match the precomputed weekly precip PNG

The archive grid is daily S2S `tp` (fetch writes per-step rates). The product
figure (`kenya-forecast-png` `weekly_precip.png`) is six weekly totals on the
Kenya product extent `7/32/-6/43`, drawn with plot's default precip palette.
Replicate it with weekly aggregation + totals, then plot (no `--colormap`):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip \
    --date 2026-08-18 -v tp -o /tmp/kenya_tp.zarr

uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/kenya_tp.zarr -o /tmp/kenya_weekly.zarr --period weekly

uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/kenya_weekly.zarr -o /tmp/kenya_weekly_mm.zarr

uv run skills/plot/scripts/plot.py -i /tmp/kenya_weekly_mm.zarr -v tp \
    --bbox 7/32/-6/43 -o /tmp/kenya_weekly.png
```

High-resolution weekly downscale (already weekly; fetch stamps
`aggregation_period`. Convert to mm then plot — no `aggregate-temporal` /
`downscale`):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip_downscaled \
    --date 2026-08-30 -o /tmp/kenya_tp_ds.zarr

uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/kenya_tp_ds.zarr -o /tmp/kenya_tp_ds_mm.zarr

uv run skills/plot/scripts/plot.py -i /tmp/kenya_tp_ds_mm.zarr -v tp \
    -o /tmp/kenya_weekly_downscaled.png
```
