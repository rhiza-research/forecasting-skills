---
name: imerg-fetch
description: Fetch live IMERG satellite precipitation for a date range and write a Rhiza Envelope Zarr. Use when a task needs recent half-hourly/daily IMERG rainfall, e.g. for station vs. satellite comparison or verification.
compatibility: Requires Python 3.12+ and uv. Requires EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment (or .netrc equivalent).
---

# imerg-fetch

Downloads IMERG late-release precipitation for the requested date range and writes a global-grid Zarr store. The IMERG late release runs ~4 days behind realtime; callers typically shift the requested end date accordingly.

## When to use

- Need recent IMERG rainfall for a forecast-verification or station-comparison task.

## Usage

```
uv run scripts/fetch.py --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range (ISO).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

Zarr with data variable `precip` (mm/hr) and dims `(time, lat, lon)` on the global IMERG 0.1° grid. Stamped with `rhiza_source=imerg`.

## Example

```bash
uv run scripts/fetch.py --start 2026-01-01 --end 2026-02-07 --output /tmp/imerg.zarr
```
