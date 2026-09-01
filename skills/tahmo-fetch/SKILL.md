---
name: tahmo-fetch
description: Fetch TAHMO station observations for specific station IDs (or all TA stations in a bbox) and write a point_obs weather-skills standard dataset Zarr. Use --list-stations --bbox N/W/S/E first to see deployment stations in a region, then --station TA00025 (repeatable) to fetch chosen ids. Use when a task needs in-situ African station rainfall/temperature/humidity/pressure, e.g. to compare against gridded satellite or forecast data.
license: MIT
compatibility: Requires Python 3.12 and uv. Installs the TAHMO Python SDK directly from GitHub (git+https://github.com/rhiza-research/tahmo-api) via uv script metadata. Requires TAHMO_API_USERNAME and TAHMO_API_PASSWORD in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - precip
    - temperature
    - humidity
    - pressure
  openclaw:
    requires:
      env:
        - TAHMO_API_USERNAME
        - TAHMO_API_PASSWORD
    primaryEnv: TAHMO_API_USERNAME
---

# tahmo-fetch

Downloads TAHMO station observations via the TAHMO SDK. Stations come from the
TAHMO **deployment API** (`getStations()`): list them in a bounding box, pick
ids, then fetch. For each `(time, variable)` it picks the best-quality sensor
(lowest TAHMO quality flag, filtering to flags <= 2), resamples to daily (sum
for `precip`, mean for `temperature`/`humidity`/`pressure`), and writes a
point_obs Zarr store.

Do not pass a country name. Resolve a place to `--bbox N/W/S/E` with
`resolve-region`, list stations in that box, then fetch the ids you want — or
fetch every TA station in the box.

## When to use

- A task needs recent daily in-situ observations for specific TAHMO stations
  (or every TA station in a region).
- A downstream skill will compare stations against gridded precip (via
  `plot-compare`) or aggregate them temporally.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox N/W/S/E
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station TA00025 [--station TA00026 ...] --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox N/W/S/E --start-time YYYY-MM-DD --end-time YYYY-MM-DD -o <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--list-stations` — query the TAHMO deployment API and print TA stations in
  `--bbox` as TSV on stdout (`station_id`, `name`, `latitude`, `longitude`,
  `country` as ISO 3166-1 alpha-2). No `-o`. Use this so the agent can choose
  which stations to fetch. An empty box prints the header and `0 stations`.
- `--station` — TAHMO station id to fetch (repeatable), e.g. `TA00025`. When
  any `--station` is given, those ids are fetched and `--bbox` is not used for
  selection.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. With `--list-stations`,
  the region to search. With a fetch and no `--station`, every TA-prefixed
  station in the box. To cover a country, get its bbox from `resolve-region`.
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`. Calendar windows: `resolve-time last-2w`. Latest published day: `--probe-latest`.
- `--probe-latest` — print the latest available `YYYY-MM-DD` on stdout and exit. No `-o`. Optional `--station` / `--bbox` restrict which stations are probed; otherwise the first few TA stations in the deployment catalogue.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — max concurrent per-station fetch threads (default 8). Stations are fetched concurrently over a bounded thread pool; lower this if TAHMO returns 429/throttling errors. Does not affect the output.

### Output

Zarr with dims `(time, station_id)`, coords `latitude(station_id)`,
`longitude(station_id)`, `country(station_id)` (ISO 3166-1 alpha-2),
`name(station_id)`, and data variables `precip` (mm day-1), `temperature`
(`degree_Celsius`), `humidity` (dimensionless fraction `1`, not percent),
`pressure` (kPa) — whichever variables the stations report.
Stamped with `weather_skills_source=tahmo` and `featureType=timeSeries`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
is the argparse namespace minus the `--input`/`--output` path strings;
`version` is the `_SKILL_VERSION` constant in `scripts/fetch.py`.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `end_time`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--end-time`). A consumer
reconstructing a `uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Examples

```bash
# 1. List stations around Nairobi so you can pick ids
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox 1.5/36.5/-1.5/37.5

# 2. Fetch the stations you chose
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station TA00025 --station TA00026 \
    --start-time 2026-01-01 --end-time 2026-02-15 -o /tmp/tahmo.zarr

# Every TA station in a country bbox (dummy; use resolve-region for a real one)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5/34/-5/42 \
    --start-time 2026-01-01 --end-time 2026-02-15 -o /tmp/tahmo_kenya.zarr
```
