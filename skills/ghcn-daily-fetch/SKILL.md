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

The output is a fully CF-1.13 timeSeries Discrete Sampling Geometries (DSG) Zarr
plus the `rhiza_history` provenance key — a superset of CF, not a separate format.

GHCN-Daily has ~130k stations and each is a separate whole-history download, so a
`--bbox` bounds the work to the stations you need; the examples below use a
bounded box and a short window.

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
  from the GHCN station metadata. Bounds the work to the stations inside the box;
  omitting it (or giving an over-wide box) selects many stations, each a separate
  whole-history download. To fetch over a country, get its bbox from the
  `resolve-region` skill.
- `--variable`, `-v` — restrict to one variable; repeat once per variable. Choices:
  `precip`, `tmax`, `tmin`, `tavg`. Omit for the default `precip tmax tmin`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--workers` — max concurrent per-station download threads (default 8). A
  concurrency knob only; it does not change the output and is excluded from the
  cache key. Lower it if the server returns throttling errors.

### Dropped-station observability

A per-station fetch that fails is retried once; if it still fails the station is
dropped and the run continues. Each drop is logged per-line, and an aggregate
count of dropped stations is printed to stderr at the end so failures are never
silently lost.

### Output

Zarr with dims `(time, station_id)`, coords `latitude(station_id)`,
`longitude(station_id)`, `name(station_id)`, and data variables among `precip`
(mm/day), `tmax`/`tmin`/`tavg` (°C) — whichever were requested and present.
GHCN-Daily values are stored in tenths (tenths of mm for PRCP, tenths of °C for
temperature) and are scaled to these canonical units on the way out. Only rows
whose quality flag is empty (passed all QC checks) are kept.

The store is fully **CF-1.13 timeSeries DSG** compliant — verified with
`cf-xarray` before writing — plus the Rhiza `rhiza_history` provenance key:

- Global attrs: `Conventions="CF-1.13"`, `featureType="timeSeries"`, plus
  `title`, `source`, `institution`, `references`, and `history`.
- `station_id` carries `cf_role="timeseries_id"` (the attr cf-xarray keys the
  geometry off).
- Every data variable carries `coordinates="latitude longitude time"`,
  udunits-valid `units`, a CF-table `standard_name`, a `long_name`,
  `cell_methods`, and an explicit `_FillValue` (in the write encoding) for the
  ragged station-time cells a station did not report.
- `latitude`/`longitude` carry `standard_name`/`units`
  (`degrees_north`/`degrees_east`); `time` carries `standard_name=time` with
  udunits `units` + `calendar` in the write encoding.
- `rhiza_source=ghcn-daily`.

Missing station-time cells are NaN (with a matching `_FillValue` carried in the
write encoding). Units are
validated against udunits before the write; if a variable's units string is not
udunits-valid the run fails rather than emit a false CF claim.

### Memory and performance

Memory scales with the number of stations inside `--bbox` times the window length
times the selected variables — station rows are small, and the working set is
bounded by the bbox, not the global station list. Per-station CSVs are fetched
concurrently and each is held only transiently while its daily values are
extracted; `--workers` raises download concurrency at a modest, bounded memory
cost. On tight-memory hosts, narrow the `--bbox` or shorten the window.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="ghcn-daily-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records `bbox`, the sorted
`variable` list, and the resolved concrete `start`/`end` — `--workers` is
excluded (a concurrency knob that does not change the data). `version` is the
`_RHIZA_SKILL_VERSION`
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
