---
name: clim-fetch
description: Fetch a precomputed daily climatology (avg + variance) for a `--dataset` (imerg_final, era5, ...) from Sheerwater's public GCS mirror and expand it onto a requested `--start-time`/`--end-time` calendar window, so timestamps line up with the rest of a pipeline's data. Optional `--bbox N/W/S/E` (compose with resolve-region) subsets before download. Use when a task needs a climatological baseline for anomalies, verification, or comparison — not live observations (use imerg-fetch, dynamical-fetch, arco-era5-fetch, etc. for those).
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
(`gs://sheerwater-public-datalake/climatologies/<product>_global1_5_global_<variable>.zarr`)
and expands it onto every calendar day in `[--start-time, --end-time]`. The
source Zarr always carries 1904 dates on its `time` dim (a leap year, so it
spans day-of-year 1..366 with no gaps); this skill re-labels each requested
date with its day-of-year and gathers the matching climatology row, repeating
rows for windows spanning more than one year.

One skill covers every mirrored climatology — the source is selected with
`--dataset`.

Grid (`global1_5`) and region (`global`) are currently hardcoded — that is
the only cache that exists today. If more grids/regions are mirrored later,
this skill will need `--grid`/`--region` flags to select among them.

## When to use

- Need a climatological baseline (mean + variance) to build an anomaly map or
  feed `difference` against live observations or a forecast.
- Want climatology output with real calendar timestamps that match another
  dataset's `time` dim, instead of a bare day-of-year axis.

Not for live observations — use `imerg-fetch` / `dynamical-fetch` for IMERG,
`arco-era5-fetch` for ERA5, etc. Not for datasets not yet mirrored — see
"Supported datasets" below.

### Supported datasets

`--dataset` must be the exact bucket product prefix — no aliasing.

| `--dataset` | Source |
| --- | --- |
| `imerg_final` | IMERG final daily precipitation climatology |
| `era5` | ERA5 daily climatology |

More datasets are added by mirroring a new Zarr under the same bucket
convention — no CLI change needed once added.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset <id> --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o <path.zarr> \
  [--variable precip] [--bbox N/W/S/E]
```

### Arguments

- `--dataset` — climatology source id (see table above).
- `--start-time`, `--end-time` — inclusive calendar window, absolute ISO dates
  `YYYY-MM-DD`. The output has one row per calendar day in this window.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--variable`, `-v` — climate variable (default: `precip`); used only as a
  fallback name if the cached Zarr has no `variable` global attr of its own.
- `--bbox` — optional `N/W/S/E` bounding box; use `resolve-region` to turn a
  country or named region into this value first. Applied before the
  day-of-year expansion, on the still-lazy remote Zarr, so only the chunks
  overlapping the bbox are pulled from GCS.

### Output

Zarr with dims `(time, lat, lon)` where `time` is exactly the requested
calendar window (real dates, not the source's 1904 placeholder). Data
variables are named `<variable>_avg` (climatological mean) and
`<variable>_variance`, where `<variable>` comes from the cached Zarr's own
`variable` global attr (e.g. `precip_avg`, `precip_variance`) — both
converted to standard display units (e.g. `mm day-1` for precip). Global
attrs include `weather_skills_source=sheerwater-mirror:<dataset>`,
`climatology_dataset`, `climatology_variable`, `climatology_grid`,
`climatology_region`. Stamped with `data_interval` `1 day`.

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
# IMERG climatology for calendar year 2020.
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py \
  --dataset imerg_final --start-time 2020-01-01 --end-time 2020-12-31 \
  -o /tmp/imerg_clim_2020.zarr

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
