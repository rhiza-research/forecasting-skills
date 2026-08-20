---
name: kenya-forecast-png
description: Fetch a pre-rendered PNG from the public Kenya forecasts archive (Filestash / gs://kenya-forecasting-data) and write it to disk. Use when a task needs an official KMSA / Sheerwater product figure (weekly, dekadal, or monthly) from https://kenya-forecasts.sheerwater.rhizaresearch.org/files/ without regenerating the plot. For analyzable grids, use kenya-forecast-fetch then plot.
license: MIT
compatibility: Requires Python 3.12 and uv. Fetches over HTTPS from the public Google Cloud Storage bucket kenya-forecasting-data; no credentials required.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: figure
  availability:
    shape: date
    policy: none
    lag_days: 0
    note: Kenya forecast PNGs; no realtime embargo
---

# kenya-forecast-png

Downloads a pre-rendered product PNG from the KMSA / Rhiza Kenya forecasts
archive and writes it to `--output`. The archive is browsable at
https://kenya-forecasts.sheerwater.rhizaresearch.org/files/; this skill reads
the same objects from the public GCS bucket that backs that browser
(`gs://kenya-forecasting-data`), so no Filestash session is required.

Layout per init date:

```
YYYY-MM-DD/<period>/<product>.png
YYYY-MM-DD/<period>/<subdir>/<product>.png
```

`--period` is one of `weekly` (default), `dekadal`, or `monthly`. `--product`
is the path under that period folder (root products like `weekly_precip.png`,
or nested like `t2m/t2m.png`).

By default the skill takes the **most recent** `YYYY-MM-DD` folder that contains
the requested product. Pass `--date` to pin an init date.

## When to use

- The user asks for a published Kenyan forecast map / outlook / meteogram PNG
  from the Sheerwater / KMSA pilot archive.
- You need the official product figure (not a re-plot from raw ensemble grids).

For raw gridded fields to chain through other weather skills (`plot`,
`plot-timeseries`, `reduce`, …), use `kenya-forecast-fetch` instead.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py -o <out.png> \
    [--date YYYY-MM-DD] [--period weekly|dekadal|monthly] [--product PATH]
```

### Arguments

- `--output`, `-o` — PNG output path (overwritten if it exists).
- `--date` — optional init date `YYYY-MM-DD`. Default: latest folder that has the
  product.
- `--period` — archive folder under `<date>/` (default `weekly`).
- `--product` — path relative to `<date>/<period>/` (default
  `weekly_precip.png`). Nested paths are allowed (`t2m/t2m.png`,
  `10m-wind/10m-wind_vectors.png`).

Common weekly precip products at the period root: `weekly_precip.png`,
`weekly_precip_downscaled.png`, `weekly_precip_downscaled_clipped.png`,
`weekly_precip_downscaled_anomaly.png`,
`weekly_precip_downscaled_anomaly_clipped.png`, `gefs_weekly_precip.png`,
`weekly_medium_range_precip.png`, `weekly_change_in_precip.png`,
`efi_sot_precip.png`. Browse the archive for variable subfolders (`t2m/`,
`cape/`, `10m-wind/`, …) and county maps under `counties/<Name>/`.

### Output

A PNG at `--output`. The decorator stamps `weather_skills_history` into the PNG
metadata. Stderr reports the resolved init date and source object key.

### Provenance

```bash
python3 -c "from PIL import Image; import json; print(json.loads(Image.open('out.png').info['weather_skills_history']))"
```

## Examples

Latest weekly precip map:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py -o /tmp/kenya_weekly_precip.png
```

Pinned init, nested t2m product:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py -o /tmp/kenya_t2m.png \
    --date 2026-08-04 --product t2m/t2m.png
```
