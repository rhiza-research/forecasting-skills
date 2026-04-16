---
name: chirps-fetch-live
description: Fetch the current-calendar-year CHIRPS v3 PRELIMINARY precipitation product (live HTTPS pull from data.chc.ucsb.edu) and write a Rhiza Envelope Zarr. Use ONLY when a task needs the most recent prelim CHIRPS data (e.g. realtime monitoring against a same-week forecast). For historical CHIRPS work — anything not in the current calendar year, including final/v2 products — use the sheerwater `chirps-fetch` skill instead.
license: MIT
compatibility: Requires Python 3.12+ and uv. The CHIRPS prelim file is fetched anonymously from data.chc.ucsb.edu — no credentials needed.
metadata:
  openclaw:
    requires:
      bins:
        - uv
---

# chirps-fetch-live

Pulls the **current calendar year** CHIRPS v3 satellite-only PRELIMINARY product live from `data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/prelim/sat/...` and writes a Zarr.

## How this differs from sheerwater's `chirps-fetch`

The sheerwater `chirps-fetch` skill (in the sheerwater repo, also named `chirps-fetch`) covers historical CHIRPS via the cached `chirps`, `chirps_v2`, `chirps_v3`, `chirp_v2`, `chirp_v3` accessors — that is the right pick for almost everything. This skill only exists because the sheerwater path does not currently expose `chirps_raw_live` (the prelim-this-year file). When sheerwater registers a live accessor (see TODO in `sheerwater.data.chirps`), this skill becomes redundant and will be removed.

If both skills are installed, route your decision by recency:

- Current calendar year, prelim acceptable → use this skill (`chirps-fetch-live`).
- Anything else (historical, finalized v3, v2, satellite-only `chirp` variants, regridded to a non-source grid, aggregated to non-daily) → use sheerwater `chirps-fetch`.

## When to use

- Realtime monitoring needing the most recent prelim CHIRPS data within the current calendar year.

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
