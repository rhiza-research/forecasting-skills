---
name: openaq-fetch
description: Fetch OpenAQ air-quality station observations (PM2.5, PM10, NO2, O3, SO2, CO) for a date range and region, and write a station-schema Rhiza Envelope Zarr. Use when a task needs in-situ air-quality / atmospheric-composition data, e.g. to compare against gridded model output.
license: MIT
compatibility: Requires Python 3.11+ and uv. Uses the OpenAQ v3 REST API over HTTPS; requires a free OPENAQ_API_KEY in the environment (register at https://explore.openaq.org/register).
metadata:
  version: "0.1.1"
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
  concurrency knob only; it does not affect the output or caching. Threads
  overlap response waits only: all requests are client-side rate-limited well
  under OpenAQ's published limits (60/minute, 2,000/hour), so
  raising `--workers` does not raise the request rate.

### Output

A fully CF-1.13 timeSeries Discrete Sampling Geometry (DSG) Zarr with dims
`(time, station_id)`, coords `latitude(station_id)`, `longitude(station_id)`,
`name(station_id)`, and data variables among `pm25`, `pm10`, `no2`, `o3`, `so2`,
`co` — whichever were requested and present. `station_id` is the OpenAQ location
id.

CF stamping:
- Global attrs `Conventions="CF-1.13"`, `featureType="timeSeries"`, plus
  `title`/`source`/`institution`/`references`/`history`.
- `station_id` carries `cf_role="timeseries_id"`; lat/lon/time carry their CF
  `standard_name`/`units`/`axis`; the time axis carries udunits reference-time
  units + a calendar in its write encoding.
- Every pollutant variable carries `coordinates="latitude longitude time"` (the
  load-bearing DSG attr), a `long_name`, `cell_methods="time: mean"` (each daily
  value is the within-day mean), and a `_FillValue` of NaN for the
  station-time cells where a sensor did not report.

Units are forwarded **verbatim** from the OpenAQ API per parameter (µg/m³ for
particulates, ppm/ppb for gases, as the provider reports) — never normalized or
converted — and are validated under udunits at write time; a unit that genuinely
fails to parse halts the run naming the parameter and unit. A CF `standard_name`
is set only where a CF-table entry cleanly matches the reported unit family: a
mass-concentration reading (µg/m³, mg/m³) gets a `mass_concentration_of_*_in_air`
name, a mole-fraction reading (ppm, ppb) a `mole_fraction_of_*_in_air` name.
Where no verified CF name applies the `standard_name` is omitted (units +
`long_name` alone is CF-valid); particulate matter has no mole-fraction CF name,
so `pm25`/`pm10` carry a `standard_name` only when reported as a mass
concentration. The store is stamped with `rhiza_source=openaq`.

### Dropped sensors

Per-sensor failures are isolated, never silently lost. A sensor whose daily
fetch fails (5xx/timeout/429) is retried once, then dropped with a per-line
stderr note. A sensor that reports a unit differing from the first-seen unit for
its pollutant is dropped too (its column cannot be merged without mislabeling),
as is a sensor with a missing/empty units value (a CF data variable cannot carry
`units=None`). At the end the run prints an aggregate count of all dropped
sensors to stderr so the loss is visible at a glance.

If a requested `-v` pollutant yields no in-window data — every one of its sensors
returned nothing or was dropped — it is omitted from the output and named in a
stderr warning; the run still succeeds with the variables that do have data.

### Errors

- `OPENAQ_API_KEY` unset: exits 2 with a clear message before any network call;
  the key value is never read or printed.
- A wrong/expired key (the API returns 401/403), whether on the locations query
  or on a per-sensor daily-values fetch (a key that expires mid-run or lacks
  `/sensors` access): a one-line actionable message and a non-zero exit; the key
  is never echoed. An auth failure is surfaced as fatal rather than counted as a
  routine per-sensor drop.
- No sensors or no observations for the bbox/window: a non-zero exit with a
  clear message.

There is no proactive size guard: `--bbox` is required (a global query would be
unbounded), but within it the caller decides the window. Request rate, however,
is guarded proactively (the client-side rate limiter above); a 429 that still
arrives is retried once after honoring its `Retry-After` header (or a 60 s
backoff without one), and other transient failures are handled reactively
(retry-once, then drop).

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
`variable` list, and the resolved concrete `start`/`end` — `--workers` is not
recorded. `version` is the value printed by `--help`. Inspect a written
output's provenance with the `provenance` skill.

## Examples

```bash
# PM2.5 for NYC-area stations over three days
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 41/-74/40/-73 --start 2024-06-01 --end 2024-06-03 \
  -v pm25 -o /tmp/openaq.zarr

# NO2 + O3 over a small bbox for the last week
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 41/-74/40/-73 --start now-1w --end now \
  -v no2 -v o3 -o /tmp/openaq_gases.zarr
```
