---
name: chirps-fetch
description: Fetch live CHIRPS precipitation observations for a date range and write a Rhiza Envelope Zarr. Use when a task needs recent CHIRPS rainfall data, e.g. to compare against a forecast or stations.
license: MIT
compatibility: Requires Python 3.12+ and uv. Fetches from the public CHIRPS prelim FTP (ftp.chc.ucsb.edu, anonymous); no credentials required.
metadata:
  version: "0.1.1"
---

# chirps-fetch

Downloads CHIRPS preliminary/final precipitation for the requested date range and writes a global-grid Zarr store.

## When to use

- A task needs recent CHIRPS rainfall as gridded observations.
- A downstream skill will clip, aggregate, or compare CHIRPS against other sources.

Not suitable for bulk historical reanalysis — only the live/preliminary CHIRPS product is fetched.

## Usage

```
uv run scripts/fetch.py --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range (ISO).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, lat, lon)` on the global CHIRPS grid. Stamped with `rhiza_source=chirps`.

### Production lag and partial-tail behavior

The CHIRPS v3.0 daily preliminary product is published on a pentad-based schedule: per-day files appear in batches **2 days after each pentad closes** (pentads end on the 5th, 10th, 15th, 20th, 25th, and last day of each month). Best-case lag is 2 days (the last day of a pentad, published 2 days later); worst case is ~7 days (the day right after a pentad ends, which waits for the next pentad to close before its batch is published). Average lag is 4-5 days. See https://www.chc.ucsb.edu/data/chirps3 for the official schedule. When the requested `--end` falls inside the lag window, the script writes a partial zarr covering only the days that were available on the FTP server, logs the missing days and effective end date to stderr, and exits 0. If days are missing from the middle of the range (not the tail), the script exits 2 — that's a server-side data gap, not a lag issue. The `rhiza_history` entry's `args.end` reflects the EFFECTIVE end actually written (not the requested `--end`), so a re-run against the same `--end` cache-misses on the partial output and re-attempts the missing tail.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="chirps-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` is the argparse namespace
minus the `--input`/`--output` path strings; `version` is the
`_RHIZA_SKILL_VERSION` constant in `scripts/fetch.py`, kept in lockstep with
`metadata.version` in this SKILL.md by the CI version-bump workflow.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `anchor_end`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--anchor-end`). A consumer
reconstructing a `uv run scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Example

```bash
uv run scripts/fetch.py --start 2026-01-01 --end 2026-02-15 --output /tmp/chirps.zarr
```
