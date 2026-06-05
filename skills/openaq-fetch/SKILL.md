---
name: openaq-fetch
description: Fetch OpenAQ air-quality station observations (PM2.5, PM10, NO2, O3, SO2, CO) for a date range and region, and write a station-schema Rhiza Envelope Zarr. Use when a task needs in-situ air-quality / atmospheric-composition data, e.g. to compare against gridded model output.
license: MIT
compatibility: Requires Python 3.11+ and uv. Uses the OpenAQ v3 REST API over HTTPS; requires a free OPENAQ_API_KEY in the environment (register at https://explore.openaq.org/register).
metadata:
  version: "0.1.0"
  openclaw:
    requires:
      env:
        - OPENAQ_API_KEY
    primaryEnv: OPENAQ_API_KEY
---

# openaq-fetch

Downloads ground-based air-quality observations from the OpenAQ v3 API. It finds
monitoring locations inside the requested bounding box, fetches each matching
sensor's daily-aggregated values over the date range concurrently, and writes a
station-schema Zarr store.

## When to use

- A task needs daily air-quality / atmospheric-composition station observations
  (PM2.5, PM10, NO2, O3, SO2, CO) for a region.
- A downstream skill will compare stations against gridded data (via
  `plot-compare`) or aggregate them temporally.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox N/W/S/E --start <date> --end <date> [-v VAR ...] -o <path.zarr>
```

Requires `OPENAQ_API_KEY` in the environment (free; register at
https://explore.openaq.org/register).

### Arguments
- `--bbox` — spatial subset `N/W/S/E` decimal degrees (required; selects which
  monitoring locations to fetch). To fetch over a country, get its bbox from the
  `resolve-region` skill.
- `--start`, `--end` — inclusive date range. Each value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — for OpenAQ this resolves to the current UTC date (the API has no
    cheap global day-precise discovery); a thin trailing tail of not-yet-reported
    days is normal;
  - an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
    days). The offset is capped at 36525 days; a larger value, a future `+`
    offset, a month/year unit, or any malformed value exits 2 before any network
    call.

  Boundary handling matches the other fetchers (inclusive both ends; duration
  idiom for `B-<int>{d|w}` .. `B`). The cache key records the resolved absolute
  dates, never the token.
- `--variable`, `-v` — restrict to one pollutant; repeat once per variable.
  Choices: `pm25`, `pm10`, `no2`, `o3`, `so2`, `co`. Omit for all six.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — max concurrent per-sensor fetch threads (default 8). A
  concurrency knob only; it does not change the output and is excluded from the
  cache key. Lower it if OpenAQ returns 429/throttling errors.

### Output

Zarr with dims `(time, station_id)`, coords `latitude(station_id)`,
`longitude(station_id)`, `name(station_id)`, and data variables among `pm25`,
`pm10`, `no2`, `o3`, `so2`, `co` — whichever were requested and present. Units
are forwarded verbatim from the OpenAQ API per parameter (e.g. µg/m³ for
particulates, ppm/ppb for gases, as the provider reports). `station_id` is the
OpenAQ location id. Stamped with `rhiza_source=openaq` and
`featureType=timeSeries`.

### Memory and performance

This is station data: memory scales with the number of sensors inside `--bbox`
times the window length times the selected pollutants, bounded by the bbox rather
than any global grid. Per-sensor daily series are fetched concurrently and held
only transiently; `--workers` raises concurrency at a modest, bounded memory cost.
On tight-memory hosts, narrow the `--bbox` or shorten the window.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="openaq-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records `bbox`, the sorted
`variable` list, and the resolved concrete `start`/`end` — `--workers` is
excluded (concurrency, not data). `version` is the `_RHIZA_SKILL_VERSION`
constant in `scripts/fetch.py`, kept in lockstep with `metadata.version` by the
CI version-bump workflow.

## Examples

```bash
# PM2.5 for NYC-area stations over three days
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 41/-74/40/-73 --start 2024-06-01 --end 2024-06-03 \
  -v pm25 -o /tmp/openaq.zarr

# All pollutants over Kenya for the last 3 weeks
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.9/-4.7/41.9 --start latest-3w --end latest \
  -o /tmp/openaq_kenya.zarr
```
