---
name: chirps-fetch
description: Fetch live CHIRPS precipitation observations for a date range and write a Rhiza Envelope Zarr. Use when a task needs recent CHIRPS rainfall data, e.g. to compare against a forecast or stations.
compatibility: Requires Python 3.12+ and uv. Credentials for CHIRPS are read from the environment or .netrc.
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

## Example

```bash
uv run scripts/fetch.py --start 2026-01-01 --end 2026-02-15 --output /tmp/chirps.zarr
```
