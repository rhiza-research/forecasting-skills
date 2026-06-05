---
name: arco-era5-fetch
description: Fetch ARCO-ERA5 reanalysis (temperature, wind, precipitation, pressure, and more) for a date range and region from the public, credential-free Google Cloud Zarr store, and write a Rhiza Envelope Zarr. Use when a task needs multi-variable gridded reanalysis ground truth for comparison, verification, or downstream clipping/aggregation/plotting.
license: MIT
compatibility: Requires Python 3.11+ and uv. Reads the public ARCO-ERA5 analysis-ready Zarr from Google Cloud (gs://gcp-public-data-arco-era5) over anonymous access; no credentials required.
metadata:
  version: "0.1.0"
---

# arco-era5-fetch

Opens the [ARCO-ERA5](https://github.com/google-research/arco-era5) analysis-ready
Zarr store (`gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3`),
subsets it by bounding box, time range, and variables, maps it onto the Rhiza
Envelope analysis shape, and writes a consolidated Zarr store. The store is a
uniform 0.25° equiangular lat/lon grid, hourly, opened with anonymous Google
Cloud access — no credentials and no API queue.

## When to use

- A task needs multi-variable reanalysis (2m temperature, winds, precipitation,
  geopotential, pressure-level fields) as gridded observations/ground truth.
- A downstream skill will clip, aggregate, compare, or plot the result as a Rhiza
  Envelope Zarr.

Not a forecast — ERA5 is reanalysis. For forecast grids use `ecmwf-fetch` or
`dynamical-fetch`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> [--bbox N/W/S/E] [-v VAR ...] -o <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — the newest date present in the store (read from its `time` coord);
  - an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
    days). The offset is capped at 36525 days; a larger value, a future `+`
    offset, a month/year unit, or any malformed value exits 2 before any network
    call.

  Boundary handling: absolute endpoints and ordinary relative ranges are
  inclusive of both ends. The **duration idiom** — start `B-<int>{d|w}` paired
  with end exactly its own base `B` (both `now`, or both `latest`) — yields an
  N-day window inclusive of `B`, with the far edge shifted in by one. For any
  invocation using a relative token, the resolved concrete window is echoed to
  stderr before fetching. `latest` discovery runs at most once per invocation and
  only when a token references `latest`. The cache key records the resolved
  absolute dates, never the relative token.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees. Longitudes are normalized
  to the [-180, 180) convention, so negative west/east values select correctly on
  ERA5's native 0..360 grid. The slice follows each axis's own order, so any
  region works. Omit to fetch the full native grid. To fetch over a country, get
  its bbox from the `resolve-region` skill.
- `--variable`, `-v` — restrict to one data variable; repeat once per variable
  (`-v 2m_temperature -v total_precipitation`). Omit to fetch all variables
  (large — ERA5 has many surface and pressure-level fields).
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A consolidated Rhiza Envelope analysis Zarr with a `time` dimension and dims
`(time, latitude, longitude)` — plus `level` when a pressure-level variable is
selected. Source variable units are forwarded verbatim. Stamped with
`rhiza_source=arco-era5`.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="arco-era5-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` is the argparse namespace
minus the `--output` path string, with the resolved concrete dates substituted
for any relative token. `version` is the `_RHIZA_SKILL_VERSION` constant in
`scripts/fetch.py`, kept in lockstep with `metadata.version` in this SKILL.md by
the CI version-bump workflow.

The `args` dict stores argparse dest names (underscored), not the hyphenated CLI
flag names. A consumer reconstructing a `uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py <args>`
invocation must translate underscore → hyphen.

## Examples

```bash
# 2m temperature over Kenya for two days
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start 2026-01-01 --end 2026-01-02 \
  --bbox 7/32/-6/43 -v 2m_temperature -o /tmp/arco.zarr

# Last week ending at the newest available time, two variables
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start latest-1w --end latest \
  --bbox 23/-20/-37/59 -v 2m_temperature -v total_precipitation -o /tmp/arco_week.zarr
```
