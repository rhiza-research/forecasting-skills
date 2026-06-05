---
name: ghcn-daily-fetch
description: Fetch NOAA GHCN-Daily global in-situ station observations (precipitation, max/min/avg temperature) for a date range and region, and write a station-schema Rhiza Envelope Zarr. Use when a task needs credential-free worldwide daily station data, e.g. to compare against gridded satellite, reanalysis, or forecast data.
license: MIT
compatibility: Requires Python 3.11+ and uv. Reads NOAA GHCN-Daily from the public S3 website endpoint (noaa-ghcn-pds.s3.amazonaws.com) over HTTPS; no credentials required.
metadata:
  version: "0.1.0"
---

# ghcn-daily-fetch

Downloads NOAA Global Historical Climatology Network daily station observations
from the public, credential-free GHCN-Daily S3 dataset. It reads the station
metadata file to find stations inside the requested bounding box, downloads each
candidate station's per-station CSV concurrently, keeps only QC-passed rows for
the requested elements and date range, scales them to canonical units, and writes
a station-schema Zarr store.

## When to use

- A task needs recent or historical daily station observations anywhere in the
  world, without credentials.
- A downstream skill will compare stations against gridded precip/temperature
  (via `plot-compare`) or aggregate them temporally.

For African stations with sub-daily sensor data, `tahmo-fetch` is an alternative
(credentialed).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--bbox N/W/S/E] --start <date> --end <date> [-v VAR ...] -o <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — for GHCN-Daily this resolves to the current UTC date. GHCN-Daily
    has no cheap day-precise discovery endpoint, and its publication lag means
    the trailing day or two may simply be absent; a missing trailing tail is
    treated as a normal partial window, not an error;
  - an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
    days). The offset is capped at 36525 days; a larger value, a future `+`
    offset, a month/year unit, or any malformed value exits 2 before any network
    call.

  Boundary handling matches the other fetchers: absolute endpoints and ordinary
  relative ranges are inclusive of both ends; the **duration idiom** (start
  `B-<int>{d|w}` with end exactly base `B`) yields an N-day window inclusive of
  `B`. The cache key records the resolved absolute dates, never the token.
- `--bbox` — spatial subset `N/W/S/E` decimal degrees, used to select stations
  from the GHCN station metadata. Omit to select ALL stations (very large — GHCN
  has 100k+ stations). To fetch over a country, get its bbox from the
  `resolve-region` skill.
- `--variable`, `-v` — restrict to one variable; repeat once per variable. Choices:
  `precip`, `tmax`, `tmin`, `tavg`. Omit for the default `precip tmax tmin`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — max concurrent per-station download threads (default 8). A
  concurrency knob only; it does not change the output and is excluded from the
  cache key. Lower it if the server returns throttling errors.

### Output

Zarr with dims `(time, station_id)`, coords `latitude(station_id)`,
`longitude(station_id)`, `name(station_id)`, and data variables among `precip`
(mm/day), `tmax`/`tmin`/`tavg` (°C) — whichever were requested and present.
GHCN-Daily values are stored in tenths (tenths of mm for PRCP, tenths of °C for
temperature) and are scaled to these canonical units on the way out. Only rows
whose quality flag is empty (passed all QC checks) are kept. Stamped with
`rhiza_source=ghcn-daily` and `featureType=timeSeries`.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="ghcn-daily-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records `bbox`, the sorted
`variable` list, and the resolved concrete `start`/`end` — `--workers` is
excluded (concurrency, not data). `version` is the `_RHIZA_SKILL_VERSION`
constant in `scripts/fetch.py`, kept in lockstep with `metadata.version` by the
CI version-bump workflow.

## Examples

```bash
# Precip + max temperature for NYC-area stations over three days
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 41/-74/40/-73 --start 2024-06-01 --end 2024-06-03 \
  -v precip -v tmax -o /tmp/ghcn.zarr

# Default variables (precip, tmax, tmin) over Kenya for the last 3 weeks
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.9/-4.7/41.9 --start latest-3w --end latest \
  -o /tmp/ghcn_kenya.zarr
```
