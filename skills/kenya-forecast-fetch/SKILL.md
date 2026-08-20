---
name: kenya-forecast-fetch
description: Fetch a raw forecast grid from the public Kenya forecasts archive (gs://kenya-forecasting-data/<date>/data/*.zarr) and write a weather-skills standard dataset Zarr. Use when a task needs analyzable Kenya pilot fields (ECMWF S2S precip/temps/winds, GEFS, medium-range precip) for clipping, aggregation, comparison, or flexible plotting via plot / plot-timeseries / plot-mediogram. For pre-rendered product PNGs, use kenya-forecast-png instead.
license: MIT
compatibility: Requires Python 3.12 and uv. Opens public consolidated Zarr over HTTPS from Google Cloud Storage bucket kenya-forecasting-data; no credentials required. Older init folders may only have GRIB/NetCDF under data/ — this skill requires Zarr.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: fetchers
  availability:
    shape: date
    policy: none
    lag_days: 0
    note: Kenya forecasts archive; no realtime embargo
---

# kenya-forecast-fetch

Opens a consolidated Zarr under the KMSA / Rhiza Kenya forecasts archive
`data/` folder, subsets it, maps it onto the weather-skills standard dataset,
and writes a local Zarr. The archive is browsable at
https://kenya-forecasts.sheerwater.rhizaresearch.org/files/; this skill reads
the same objects from `gs://kenya-forecasting-data` over HTTPS.

Layout:

```
YYYY-MM-DD/data/ECMWF_s2s_precip_YYYY-MM-DD.zarr/
YYYY-MM-DD/data/medium_range_precip.zarr/
YYYY-MM-DD/data/gefs/gefs_kenya.zarr/
…
```

By default the skill takes the **most recent** init date that has the requested
`--dataset`. Pass `--date` to pin an init.

This skill does **not** plot. For flexible figures, chain to `plot`,
`plot-timeseries`, `plot-mediogram`, `reduce`, `clip-region`, etc. For the
archive's pre-rendered product PNGs, use `kenya-forecast-png`.

## When to use

- Need Kenya-region ensemble / medium-range grids already published in the
  pilot archive (no ECDS queue).
- Downstream analysis or custom plots from those grids.

Prefer `ecmwf-fetch` / `dynamical-fetch` when you need a live global fetch or an
init date whose `data/` folder only has legacy GRIB/NetCDF (no `.zarr`).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset <id> \
    [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>
```

### Datasets

| `--dataset` | Store under `<date>/data/` | Typical vars |
|---|---|---|
| `precip` (default) | `ECMWF_s2s_precip_<date>.zarr` | `tp` |
| `daily_vars` | `ECMWF_s2s_daily_vars_<date>.zarr` | `t2m`, `d2m`, `cape`, `tcw` |
| `Tminmax` | `ECMWF_s2s_Tminmax_<date>.zarr` | `mn2t6`, `mx2t6` |
| `10wind` | `ECMWF_s2s_10wind_<date>.zarr` | `u10`, `v10` |
| `500wind` | `ECMWF_s2s_500wind_<date>.zarr` | `w` (and related) |
| `700wind` | `ECMWF_s2s_700wind_<date>.zarr` | `u`, `v` |
| `medium_range_precip` | `medium_range_precip.zarr` | `tp` |
| `gefs` | `gefs/gefs_kenya.zarr` | `tp` |

Output dims follow the classic forecast shape: scalar `time` (init) + `step`
(+ `number` for ensembles) + `latitude`/`longitude`. Precipitation variables
arrive as **amounts** (`lwe_thickness_of_precipitation_amount`, `mm`); use
`deaccumulate` when you need per-step rates for middle-pipeline skills that
expect `mm day-1`.

### Arguments

- `--dataset` — product id from the table (default `precip`).
- `--date` — optional init date `YYYY-MM-DD`. Default: latest folder with that
  Zarr.
- `--bbox` — optional spatial subset `N/W/S/E`.
- `--variable`, `-v` — restrict to named data variables (repeatable).
- `--output`, `-o` — output Zarr path.

### Example: fetch then plot

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip \
    --date 2026-08-04 -v tp -o /tmp/kenya_tp.zarr

# Heatmap: ensemble-mean panels (plot averages `number`); restrict to one lead:
uv run skills/plot/scripts/plot.py -i /tmp/kenya_tp.zarr -v tp \
    --index step=7 -o /tmp/kenya_tp_week1.png
```
