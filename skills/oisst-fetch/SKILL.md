---
name: oisst-fetch
description: Fetch NOAA OISST v2.1 daily sea-surface temperature for a date range and region from NOAA PSL's public OPeNDAP server, and write a weather-skills envelope Zarr. Use when a task needs credential-free gridded SST observations, e.g. for ocean analysis or comparison against forecasts/reanalysis.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads NOAA OISST v2.1 from NOAA PSL's OPeNDAP server (psl.noaa.gov) over HTTPS; no credentials required.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.5"
  catalog-group: fetchers
---

# oisst-fetch

Reads NOAA's Optimum Interpolation Sea Surface Temperature (OISST) v2.1 daily
0.25° global analysis from NOAA PSL's public OPeNDAP server, subsets it by
bounding box and time range, maps it onto the weather-skills envelope analysis shape, and
writes a consolidated Zarr store. OPeNDAP lets the skill pull only the requested
window rather than whole yearly files, with no credentials.

## When to use

- A task needs gridded sea-surface temperature observations (daily, 0.25°,
  global, 1981-09 to present), without credentials.
- A downstream skill will clip, aggregate, compare, or plot the result as a weather-skills
  envelope Zarr.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start <date> --end <date> [--bbox N/W/S/E] -o <path.zarr>
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

A consolidated weather-skills envelope analysis Zarr with a `time` dimension and dims
`(time, latitude, longitude)`, carrying `sst` (sea-surface temperature, °C).
Land cells are NaN. Stamped with `weather_skills_source=oisst`.

The store is fully **CF-1.13** compliant (the weather-skills envelope is a CF superset —
CF plus the `weather_skills_history` provenance key):

- Global attrs: `Conventions="CF-1.13"`, `title`, `source` (NOAA OISST v2.1, read
  over NOAA PSL OPeNDAP), `institution`, `references`, `history`.
- `latitude`/`longitude`: `standard_name`, `units` (`degrees_north`/
  `degrees_east`), `axis` (`Y`/`X`).
- `time`: `standard_name=time`, `axis=T`, with an explicit udunits `units` and
  `calendar` in the write encoding.
- `sst`: `standard_name=sea_surface_temperature`, `long_name`, `units` (`degC`,
  validated as a udunits temperature unit), and an explicit NaN `_FillValue` in
  the encoding for land cells.

cf-xarray resolves the X/Y/T axes on the output, and the write is gated on that
decode succeeding.

### Memory and performance

There is one variable (`sst`); `--bbox` and the window length are the memory
levers. The output is streamed one year at a time — each year's bbox selection is
loaded, written, and released before the next — so peak resident memory is bounded
to a single year's selection (the full global grid is ~720×1440, roughly 4 MB per
day as float32). Keep `--bbox` tight and the window short; run the `clip-region`
skill afterward to trim further.

### Errors

OISST is served over NOAA PSL's OPeNDAP server, which limits request size. The
skill reports failures reactively — it does not pre-estimate request size or
refuse against any threshold — so the messages below appear only after the
provider or the resolver fails:

- **Transport / oversized request** — the data transfer is rejected at load time
  (a DAP failure, typically a large `--bbox` over a long window). The message
  names the remedy: reduce `--bbox` and/or shorten the date range; this is not a
  credentials or availability problem, so retrying the same request will not help.
- **Availability** — a year file cannot be opened. The year may be outside the
  served range (1981-09 to present) or the server is unreachable; check the date
  range. Distinct from the oversized case.
- **Empty window** — the resolved window is in range but contains no data; exits
  with a "no data in window" message, kept distinct from a failure.
- **`latest` resolution** — when neither the current nor previous year file can be
  read, the message distinguishes a transport failure (server unreachable) from a
  genuine absence of the year files (use an absolute `--start`/`--end` in the
  served range instead).

Exit codes: exit `2` covers two groups — the pre-network argument/parse errors
(a malformed `--start`/`--end` date token, a reversed resolved range, or a
malformed `--bbox`) and the load-time data-transfer failures on a year's slice
(transport, availability, or an unexpected read failure). Exit `1` covers the
remaining failures: the empty-window case (no data in the resolved range), a
`latest` resolution failure, a year-file open failure, a `--bbox` that selects
no grid cells, and a CF stamping or decode failure.

If a multi-year window fails partway through (a later year's transfer fails after
an earlier year was written), the partial store is removed before the non-zero
exit, so a later identical run does not falsely accept it as a cache hit. The
cache-hit check also reads back a corner of `sst` to confirm the store is complete
before honoring a hit.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For this fetcher it is a
length-1 array with `skill="oisst-fetch"` and `input=null`; downstream
zarr-writing skills append their own entry. `args` records the `bbox` and the
resolved concrete `start`/`end`. `version` is this skill's version, also printed
by `--help`. Inspect a written output's provenance with the `provenance` skill.

## Examples

```bash
# SST over the seas around East Africa for three days
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 7/32/-6/43 --start 2024-06-01 --end 2024-06-03 \
  -o /tmp/oisst.zarr

# Last 3 weeks ending at the newest available day, over a bounded region
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 7/32/-6/43 --start latest-3w --end latest \
  -o /tmp/oisst_week.zarr
```
