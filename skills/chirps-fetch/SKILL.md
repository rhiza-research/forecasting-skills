---
name: chirps-fetch
description: Fetch CHIRPS precipitation observations for a date range — the validated final product back to 1998, with a preliminary fallback for very recent days — and write a Rhiza Envelope Zarr. Use when a task needs CHIRPS rainfall, recent or historical, e.g. to compare against a forecast or stations or to build a reference period.
license: MIT
compatibility: Requires Python 3.12+ and uv. Fetches over HTTPS from the public CHIRPS data server (data.chc.ucsb.edu); no credentials required.
metadata:
  version: "0.1.10"
---

# chirps-fetch

Downloads CHIRPS v3.0 daily `sat` precipitation for the requested date range and writes a global-grid Zarr store. Each day is taken from the validated **final** product (a per-year archive covering 1998 to present) when available, falling back to the **preliminary** product for very recent days the final has not finalized yet. When both exist for a day, final is used.

## When to use

- A task needs CHIRPS rainfall as gridded observations — recent days, a historical period, or a reference/normal year (final coverage runs 1998 to present).
- A downstream skill will clip, aggregate, or compare CHIRPS against other sources.

Coverage starts in 1998 (CHIRPS v3.0 `sat`); dates before 1998 are unavailable and exit non-zero.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — the newest available CHIRPS prelim day (found by probing the data
    server backward day-by-day over HTTPS to the first published day);
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
- `--workers` — max concurrent per-day download threads (default 8). Bounds the thread pool that fetches each day's TIF over HTTPS. A concurrency knob only, not data: it is excluded from the cache key. Lower it if the CHIRPS server returns throttling errors.

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, latitude, longitude)` on the global CHIRPS grid. Stamped with `rhiza_source=chirps`.

### Memory and performance

There is no `--bbox` flag: the full 0.05° global grid (~7200×3600 cells, ~104 MB/day as float32) is always fetched. The output is streamed to Zarr one day at a time, so peak resident memory is bounded to ~one day regardless of how long the window is.

`--workers` is the network-concurrency speed lever and is memory-neutral: each worker transiently holds only the compressed TIF body (a few MB), not a decompressed global array — decompression happens sequentially after the download pool drains. Raising `--workers` speeds the fetch without raising peak memory.

All per-day TIFs are staged to a temp directory before writing, so a very long window is bounded by temp disk, not RAM. For tight-memory hosts, keep the window short and run the `clip-region` skill immediately after to shrink to your area of interest.

### Production lag and partial-tail behavior

Historical days come from the validated final product and carry no publication lag; the lag and partial-tail behavior described here apply only to the recent tail, which is served by the preliminary product. The CHIRPS v3.0 daily preliminary product is published on a pentad-based schedule: per-day files appear in batches **2 days after each pentad closes** (pentads end on the 5th, 10th, 15th, 20th, 25th, and last day of each month). Best-case lag is 2 days (the last day of a pentad, published 2 days later); worst case is ~7 days (the day right after a pentad ends, which waits for the next pentad to close before its batch is published). Average lag is 4-5 days. See https://www.chc.ucsb.edu/data/chirps3 for the official schedule. When the requested `--end` falls inside the lag window, the script writes a partial zarr covering only the days that were available on the server, logs the missing days and effective end date to stderr, and exits 0. If days are missing from the middle of the range (not the tail), the script exits 2 — that's a server-side data gap, not a lag issue. The `rhiza_history` entry's `args.end` reflects the EFFECTIVE end actually written (not the requested `--end`), so a re-run against the same `--end` cache-misses on the partial output and re-attempts the missing tail.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="chirps-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records the run's flag
values under underscored names (e.g. a flag `--time-dim` is recorded as
`time_dim`); `version` is the value printed by `--help`. Inspect a written
output's provenance with the `provenance` skill.

## Example

```bash
# Absolute window (inclusive both ends)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start 2026-01-01 --end 2026-02-15 --output /tmp/chirps.zarr

# Last 3 weeks ending at the newest available CHIRPS day (duration idiom: 21 days incl. latest)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start latest-3w --end latest --output /tmp/chirps.zarr
```
