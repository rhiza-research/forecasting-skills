---
name: tahmo-fetch
description: Fetch TAHMO station observations for one or more African countries and write a weather-skills envelope Zarr (station-dim schema). Use when a task needs in-situ station rainfall/temperature/humidity/pressure, e.g. to compare against gridded satellite or forecast data.
license: MIT
compatibility: Requires Python 3.10+ and uv. Installs the TAHMO Python SDK directly from GitHub (git+https://github.com/rhiza-research/tahmo-api) via uv script metadata. Requires TAHMO_API_USERNAME and TAHMO_API_PASSWORD in the environment.
metadata:
  version: "0.1.8"
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
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --country Kenya [--country Ghana ...] --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--country` — country name (pass once per country). Supported: Kenya, Ghana, Senegal, Ethiopia, Burkina Faso, Benin, DR Congo, Côte d'Ivoire, Cameroon, Lesotho, Madagascar, Mali, Malawi, Mozambique, Niger, Nigeria, Rwanda, Chad, Togo, Tanzania, Uganda, South Africa, Zambia, Zimbabwe.
- `--start`, `--end` — inclusive date range. Each value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — the newest available TAHMO observation date (found by requesting
    controlled raw data over a bounded lookback ending today and taking the max
    returned observation date across the requested countries' stations).
    Observations are filtered to on-or-before today (UTC) before the max is
    taken, so `latest` is always a real observation day on or before today. If
    every candidate station fails to respond (an auth/transport problem) the run
    exits non-zero with that error rather than reporting no data; only when
    stations respond but carry no in-window observation is the no-data case
    reported;
  - an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
    days, so `3w` = 21 days). The offset is capped at 36525 days; a larger value,
    a future `+` offset, a month/year unit, or any malformed value exits 2 before
    any network call.

  Boundary handling: absolute endpoints and ordinary relative ranges are
  inclusive of both ends. The **duration idiom** — start `B-<int>{d|w}` paired
  with end exactly its own base `B` (both `now`, or both `latest`) — yields an
  N-day window inclusive of `B`, with the far edge shifted in by one. So
  `latest-3w .. latest` resolves to `[latest-20d, latest]` = 21 days incl.
  `latest`, and `now-1w .. now` resolves to 7 days; `2026-05-01 .. 2026-05-10` is
  10 days. Tokens stay literal (`latest-3w` = `latest − 21d`); only the
  `B-N .. B` shape moves the far edge. After resolution, `start <= end` or the
  run exits 2 (pre-network). For any invocation using a relative token, the
  resolved concrete window is echoed to stderr before fetching, e.g.
  `resolved "latest-3w".."latest" -> 2026-05-10..2026-05-30 (21 days; duration mode: 3-week window inclusive of latest)`
  or `... (inclusive both ends)`. `latest` discovery runs at most once and only
  when a token references `latest`. The cache key records the resolved absolute
  dates, never the relative token.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — max concurrent per-station fetch threads (default 8). Stations are fetched concurrently over a bounded thread pool; lower this if TAHMO returns 429/throttling errors. Does not affect the output or the cache key.

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
reconstructing a `uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Example

```bash
# Absolute window (inclusive both ends)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --country Kenya --country Ghana --start 2026-01-01 --end 2026-02-15 \
    --output /tmp/tahmo.zarr

# Last 3 weeks ending at the newest available observation (duration idiom: 21 days incl. latest)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --country Kenya --start latest-3w --end latest --output /tmp/tahmo.zarr
```
