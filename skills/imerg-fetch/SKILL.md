---
name: imerg-fetch
description: Fetch live IMERG satellite precipitation for a date range and write a weather-skills envelope Zarr. Use when a task needs recent half-hourly/daily IMERG rainfall, e.g. for station-vs-satellite comparison or verification.
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.12"
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
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> --output <path.zarr> [--version late|final]
```

`--start` and `--end` accept either an absolute ISO date or a relative token
(see below). The window is inclusive of both ends.

### Arguments
- `--start`, `--end` — inclusive date range. Each value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — the newest available IMERG granule date (discovered via
    `earthaccess`, so the caller never guesses the production lag);
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
  run exits 2 (pre-network).

  For any invocation using a relative token, the resolved concrete window is
  echoed to stderr before fetching, e.g.
  `resolved "latest-3w".."latest" -> 2026-05-10..2026-05-30 (21 days; duration mode: 3-week window inclusive of latest)`
  or `... (inclusive both ends)`. `latest` discovery runs at most once per
  invocation and only when a token references `latest`; an all-absolute or
  `now`-only window performs no discovery call.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--version` — `late` (default; ~4 days behind realtime, `GPM_3IMERGDL`) or `final` (`GPM_3IMERGDF`).

The provenance/cache `args` records the resolved concrete `{start, end, version}`,
never the relative token, so the same resolved window cache-hits and a relative
spec never false-hits across days.

### Short-window warning

IMERG late runs ~4 days behind realtime, so a window whose end is at or near
today (e.g. `--end now`) can resolve to a span whose trailing days are not yet
published. After the fetch, the present days are read from the written dataset's
own time axis (not from CMR metadata), and a span with fewer present days than
the requested span is classified as follows:

- A contiguous **trailing** gap (the missing days are exactly the tail past the
  last present day) prints a stderr `WARNING` and stamps the cache key with the
  **effective end** (the last day actually present) rather than the requested
  end. A later run for the same requested window therefore misses the cache and
  re-fetches the now-published tail instead of short-circuiting on a
  partial-window cache hit.
- An **interior** hole (a missing day that precedes a later present day) is a
  server/data gap rather than realtime lag, so the run exits non-zero rather
  than silently caching a window with a hole in the middle.

If no granule day falls inside the resolved window at all, the run exits
non-zero.

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, latitude, longitude)` on the global IMERG 0.1° grid. Stamped with `weather_skills_source=imerg`.

### Memory and performance

There is no `--bbox` flag: the full 0.1° global grid (~3600×1800 cells, ~26 MB/day as float32) is always fetched. The output is streamed to Zarr one granule (one day) at a time, so peak resident memory is bounded regardless of window length.

There is no spend-RAM-to-go-faster knob: the wall-clock cost is the NASA Earthdata download (which `earthaccess` parallelizes internally), not the streamed write, so extra memory does not speed up the fetch.

For tight-memory hosts, keep the window short and run the `clip-region` skill immediately after to shrink to your area of interest.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
records the resolved concrete window as `{start, end, version}` — the resolved
absolute dates, not the relative token. `version` is the value printed by
`--help`. Inspect a written output's provenance with the `provenance` skill.

## Example

```bash
# Absolute window (10 inclusive days)
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start 2026-05-01 --end 2026-05-10 --output /tmp/imerg.zarr

# Last 3 weeks ending at the latest available granule (duration idiom: 21 days incl. latest)
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start latest-3w --end latest --output /tmp/imerg.zarr

# From a fixed start through today (inclusive both ends)
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start 2026-06-01 --end now --output /tmp/imerg.zarr
```
