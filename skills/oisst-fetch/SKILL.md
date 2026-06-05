---
name: oisst-fetch
description: Fetch NOAA OISST v2.1 daily sea-surface temperature for a date range and region from NOAA PSL's public OPeNDAP server, and write a Rhiza Envelope Zarr. Use when a task needs credential-free gridded SST observations, e.g. for ocean analysis or comparison against forecasts/reanalysis.
license: MIT
compatibility: Requires Python 3.11+ and uv. Reads NOAA OISST v2.1 from NOAA PSL's OPeNDAP server (psl.noaa.gov) over HTTPS; no credentials required.
metadata:
  version: "0.1.0"
---

# oisst-fetch

Reads NOAA's Optimum Interpolation Sea Surface Temperature (OISST) v2.1 daily
0.25° global analysis from NOAA PSL's public OPeNDAP server, subsets it by
bounding box and time range, maps it onto the Rhiza Envelope analysis shape, and
writes a consolidated Zarr store. OPeNDAP lets the skill pull only the requested
window rather than whole yearly files, with no credentials.

## When to use

- A task needs gridded sea-surface temperature observations (daily, 0.25°,
  global, 1981-09 to present), without credentials.
- A downstream skill will clip, aggregate, compare, or plot the result as a Rhiza
  Envelope Zarr.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> [--bbox N/W/S/E] -o <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — the newest day present in the current-year OISST file (the product
    runs about a day behind realtime);
  - an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
    days). The offset is capped at 36525 days; a larger value, a future `+`
    offset, a month/year unit, or any malformed value exits 2 before any network
    call.

  Boundary handling matches the other fetchers: absolute endpoints and ordinary
  relative ranges are inclusive of both ends; the **duration idiom** (start
  `B-<int>{d|w}` with end exactly base `B`) yields an N-day window inclusive of
  `B`. For a relative token the resolved concrete window is echoed to stderr. The
  cache key records the resolved absolute dates, never the token.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. Longitudes are normalized
  to the [-180, 180) convention so negative west/east values select correctly on
  OISST's native 0..360 grid. Omit for the full global grid. To fetch over a
  region, get its bbox from the `resolve-region` skill.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated Rhiza Envelope analysis Zarr with a `time` dimension and dims
`(time, latitude, longitude)`, carrying `sst` (sea-surface temperature, °C).
Land cells are NaN. Stamped with `rhiza_source=oisst`.

### Memory and performance

There is one variable (`sst`); `--bbox` and the window length are the memory
levers. The output is streamed one year at a time — each year's bbox selection is
loaded, written, and released before the next — so peak resident memory is bounded
to a single year's selection regardless of how many years the window spans (the
full global grid is ~720×1440, roughly 4 MB per day as float32). On tight-memory
hosts keep `--bbox` tight and the window short, and run the `clip-region` skill
afterward.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="oisst-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records the `bbox` and the
resolved concrete `start`/`end`. `version` is the `_RHIZA_SKILL_VERSION` constant
in `scripts/fetch.py`, kept in lockstep with `metadata.version` by the CI
version-bump workflow.

## Examples

```bash
# SST over the seas around East Africa for three days
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 7/32/-6/43 --start 2024-06-01 --end 2024-06-03 \
  -o /tmp/oisst.zarr

# Last 3 weeks ending at the newest available day, full global grid
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start latest-3w --end latest -o /tmp/oisst_week.zarr
```
