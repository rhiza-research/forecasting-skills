---
name: chirps-fetch
description: Fetch CHIRPS precipitation observations for a date range — the validated final product back to 1998, with a preliminary fallback for very recent days — and write a weather-skills standard dataset Zarr. Use when a task needs CHIRPS rainfall, recent or historical, e.g. to compare against a forecast or station data, or to build a reference period.
license: MIT
compatibility: Requires Python 3.12 and uv. Fetches over HTTPS from the public CHIRPS data server (data.chc.ucsb.edu); no credentials required.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.18"
  catalog-group: fetchers
---

# chirps-fetch

Downloads CHIRPS v3.0 daily `sat` precipitation for the requested date range and writes a global-grid Zarr store. Each day is taken from the validated **final** product (a per-year archive covering 1998 to present) when available, falling back to the **preliminary** product for very recent days the final has not finalized yet. When both exist for a day, final is used.

## When to use

- A task needs CHIRPS rainfall as gridded observations — recent days, a historical period, or a reference/normal year (final coverage runs 1998 to present).
- A downstream skill will clip, aggregate, or compare CHIRPS against other sources.

Coverage starts in 1998 (CHIRPS v3.0 `sat`); dates before 1998 are unavailable and exit non-zero.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — max concurrent per-day download threads (default 2). Bounds the thread pool that fetches each day's TIF over HTTPS. The default is deliberately conservative: CHC's data server can throttle and temporarily block IPs under higher concurrency. If throttling errors appear, lower it to 1. If requests are refused outright, the IP may be temporarily blocked — wait before retrying; lowering `--workers` helps only before a block.

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, latitude, longitude)` on the global CHIRPS grid. Stamped with `weather_skills_source=chirps`.

### Memory and performance

There is no `--bbox` flag: the full 0.05° global grid (~7200×3600 cells, ~104 MB/day as float32) is always fetched. The skill builds the full window in memory before writing.

`--workers` is the network-concurrency speed lever and is memory-neutral: each worker transiently holds only the compressed TIF body (a few MB), not a decompressed global array — decompression happens sequentially after the download pool drains. The default is held low because CHC's data server can throttle and temporarily block IPs; raising `--workers` is an operator choice for infrastructure where that risk is acceptable (see the `--workers` argument above).

All per-day TIFs are staged to a temp directory before writing, so a very long window is bounded by temp disk, not RAM. For tight-memory hosts, keep the window short and run the `clip-region` skill immediately after to shrink to your area of interest.

### Production lag and partial-tail behavior

Historical days come from the validated final product and carry no publication lag; the lag and partial-tail behavior described here apply only to the recent tail, which is served by the preliminary product. The CHIRPS v3.0 daily preliminary product is published on a pentad-based schedule: per-day files appear in batches **2 days after each pentad closes** (pentads end on the 5th, 10th, 15th, 20th, 25th, and last day of each month). Best-case lag is 2 days (the last day of a pentad, published 2 days later); worst case is ~7 days (the day right after a pentad ends, which waits for the next pentad to close before its batch is published). Average lag is 4-5 days. See https://www.chc.ucsb.edu/data/chirps3 for the official schedule. When the requested `--end-time` falls inside the lag window, the script writes a partial dataset covering only the days that were available on the server, logs the missing days and effective end date to stderr, and exits 0. If days are missing from the middle of the range (not the tail), the script exits 2 — that's a server-side data gap, not a lag issue.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="chirps-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records the run's flag
values under underscored names (e.g. a flag `--time-dim` is recorded as
`time_dim`); `version` is the value printed by `--help`. Inspect a written
output's provenance with the `provenance` skill.

## Example

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-01-01 --end-time 2026-02-15 --output /tmp/chirps.zarr
```
