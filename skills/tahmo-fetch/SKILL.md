---
name: tahmo-fetch
description: Fetch TAHMO station observations for one or more African countries and write a weather-skills envelope Zarr (station schema). Use when a task needs in-situ station rainfall/temperature/humidity/pressure, e.g. to compare against gridded satellite or forecast data.
license: MIT
compatibility: Requires Python 3.12 and uv. Installs the TAHMO Python SDK directly from GitHub (git+https://github.com/rhiza-research/tahmo-api) via uv script metadata. Requires TAHMO_API_USERNAME and TAHMO_API_PASSWORD in the environment.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.14"
  catalog-group: fetchers
  openclaw:
    requires:
      env:
        - TAHMO_API_USERNAME
        - TAHMO_API_PASSWORD
    primaryEnv: TAHMO_API_USERNAME
---

# tahmo-fetch

Downloads TAHMO station observations via the TAHMO SDK for the requested countries and date range. For each `(time, variable)` it picks the best-quality sensor (lowest TAHMO quality flag, filtering to flags <= 2), then resamples to daily (sum for `precip`, mean for `temperature`/`humidity`/`pressure`), and writes a station-schema Zarr store.

## When to use

- A task needs recent daily station observations for one or more named African countries.
- A downstream skill will compare stations against gridded precip (via `plot-compare`) or aggregate them temporally.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --country Kenya [--country Ghana ...] --start-time YYYY-MM-DD --end-time YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--country` — country name (pass once per country). Supported: Kenya, Ghana, Senegal, Ethiopia, Burkina Faso, Benin, DR Congo, Côte d'Ivoire, Cameroon, Lesotho, Madagascar, Mali, Malawi, Mozambique, Niger, Nigeria, Rwanda, Chad, Togo, Tanzania, Uganda, South Africa, Zambia, Zimbabwe.
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — max concurrent per-station fetch threads (default 8). Stations are fetched concurrently over a bounded thread pool; lower this if TAHMO returns 429/throttling errors. Does not affect the output.

### Output

Zarr with dims `(time, station_id)`, coords `latitude(station_id)`, `longitude(station_id)`, `country(station_id)`, and data variables `precip` (mm/day), `temperature` (°C), `humidity` (%), `pressure` (kPa) — whichever variables the stations report. Stamped with `weather_skills_source=tahmo` and `featureType=timeSeries`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
is the argparse namespace minus the `--input`/`--output` path strings;
`version` is the `_SKILL_VERSION` constant in `scripts/fetch.py`, kept
in lockstep with `metadata.version` in this SKILL.md by the CI version-bump
workflow.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `anchor_end`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--anchor-end`). A consumer
reconstructing a `uv run --script ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Example

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --country Kenya --country Ghana --start-time 2026-01-01 --end-time 2026-02-15 \
    --output /tmp/tahmo.zarr
```
