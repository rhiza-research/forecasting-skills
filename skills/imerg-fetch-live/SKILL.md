---
name: imerg-fetch-live
description: Fetch IMERG (early/late/final) precipitation directly from NASA Earthaccess with recompute=True (always live, never cached) and write a Rhiza Envelope Zarr. Use ONLY when freshness matters more than cache speed — e.g. realtime monitoring of the late run within the last few days. For historical IMERG work, use the sheerwater `imerg-fetch` skill instead.
license: MIT
compatibility: Requires Python 3.12+ and uv. Requires EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment (or .netrc equivalent).
metadata:
  openclaw:
    requires:
      bins:
        - uv
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# imerg-fetch-live

Downloads IMERG precipitation per-fetch via `imerg_raw_live` with `recompute=True` and `cache_mode="local_overwrite"` — the cache is actively bypassed each call. Supports `--version early|late|final`. The late release runs ~4 days behind realtime; callers typically shift the requested end date accordingly.

## How this differs from sheerwater's `imerg-fetch`

The sheerwater `imerg-fetch` skill (in the sheerwater repo, also named `imerg-fetch`) uses the cached `imerg`, `imerg_final`, and `imerg_late` accessors which always read from sheerwater's year-aggregated cache first — fast for historical work, but won't pick up data fresher than the last cache refresh.

This skill calls the live earthaccess path and ignores the cache, so it's slower but always returns the most recent data NASA has published. When sheerwater registers `imerg_raw_live` as an accessor (see TODO in `sheerwater.data.imerg`), this skill becomes redundant and will be removed.

If both skills are installed, route by freshness:

- "I need today's or this week's IMERG late and the cache may not have it yet" → this skill (`imerg-fetch-live`).
- Anything else (any historical date, final run more than a few months old, or you want the cache speedup) → sheerwater `imerg-fetch`.

You also need your own `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` to use this skill — the sheerwater path uses sheerwater's managed credentials so end users don't need NASA accounts.

## When to use

- Realtime monitoring needing IMERG data fresher than the sheerwater cache.

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
