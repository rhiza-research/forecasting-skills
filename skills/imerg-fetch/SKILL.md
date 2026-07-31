---
name: imerg-fetch
description: Fetch live IMERG satellite precipitation for a date range and write a weather-skills standard dataset Zarr. Use when a task needs recent half-hourly/daily IMERG rainfall, e.g. for station-vs-satellite comparison or verification.
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.14"
  catalog-group: fetchers
  openclaw:
    requires:
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# imerg-fetch

Downloads IMERG daily precipitation granules from NASA GES DISC via `earthaccess` for the requested date range and writes a global-grid Zarr store. The IMERG late release runs ~4 days behind realtime; callers typically shift the requested end date accordingly.

## When to use

- Need recent IMERG rainfall for a forecast-verification or station-comparison task.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD --output <path.zarr> [--version late|final]
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--version` — `late` (default; ~4 days behind realtime, `GPM_3IMERGDL`) or `final` (`GPM_3IMERGDF`).

### Production lag and partial-tail behavior

IMERG late runs ~4 days behind realtime, so a window whose `--end-time` is at or near
the present can include trailing days not yet published. After the fetch, the
present days are read from the written dataset's own time axis, and a span with
fewer present days than requested is classified as follows:

- A contiguous **trailing** gap (missing days are exactly the tail past the last
  present day) prints a stderr `WARNING` naming the missing days and effective
  end date; the run exits 0 with a partial dataset.
- An **interior** hole (a missing day that precedes a later present day) is a
  server/data gap rather than realtime lag, so the run exits non-zero.

If no granule day falls inside the requested window at all, the run exits
non-zero.

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, latitude, longitude)` on the global IMERG 0.1° grid. Stamped with `weather_skills_source=imerg`.

### Memory and performance

There is no `--bbox` flag: the full 0.1° global grid (~3600×1800 cells, ~26 MB/day as float32) is always fetched. The skill builds the full window in memory before writing.

For tight-memory hosts, keep the window short and run the `clip-region` skill immediately after to shrink to your area of interest.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
records `{start, end, version}`. `version` is the value printed by
`--help`. Inspect a written output's provenance with the `provenance` skill.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-05-01 --end-time 2026-05-10 --output /tmp/imerg.zarr
```
