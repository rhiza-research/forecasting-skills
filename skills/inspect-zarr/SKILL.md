---
name: inspect-zarr
description: Inspect a weather-skills standard dataset Zarr — print dimension sizes, coordinate values, and a data-variable summary (names, dims, dtype, units). Use when you need to see what is in a Zarr before clipping, selecting, aggregating, or plotting, or to confirm lat/lon/time coordinates after a fetch.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/inspect.py *)
metadata:
  catalog-group: agent-tooling
---

# inspect-zarr

Read-only dump of a Zarr store's structure. Prints dimension sizes, every
coordinate (with values), and a one-line summary of each data variable. It
does not print data-variable arrays (those can be huge) and it does not write
an output store.

Use `provenance` when you need the `weather_skills_history` lineage rather than
the grid itself.

## When to use

- Checking which dims and coordinates a fetch or transform produced.
- Reading lat/lon/time (or `step` / `number`) values before `select`,
  `clip-region`, `aggregate-temporal`, or `plot`.
- Confirming units on data variables after `unit-convert` or
  `convert-to-totals`, and whether `data_interval` / `aggregation_period` /
  `aggregation_coverage` are present.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect.py --input <in.zarr> \
    [--format human|json] [--max-values N]
```

### Arguments

- `--input`, `-i` — Zarr to inspect (`Dataset("any")`; any weather-skills
  store, including precip totals).
- `--format` — `human` (default) or `json`.
- `--max-values` — maximum coordinate values printed per coordinate (default
  24). Long axes print a head/tail preview and the total count. `0` prints
  every value.

Takes no `--output`. Nothing is written; stdout is the result.

### Output

**human**

```
Dimensions:
  time: 2
  latitude: 3
  longitude: 4

Coordinates:
  time (time) datetime64[ns]
    2026-01-01, 2026-01-02
  latitude (latitude) float64 [degrees_north]
    1, 2, 3
  longitude (longitude) float64 [degrees_east]
    10, 11, 12, 13

Data variables:
  precip (time, latitude, longitude) float64 2 × 3 × 4 [mm d-1]
```

A truncated coordinate line ends with `(N values)`.

**json** — the same information as `dims`, `coords` (`values` / `truncated` /
`size`), and `data_vars`. Use `provenance` for `weather_skills_history`.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect.py -i /tmp/ecmwf.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect.py -i /tmp/ecmwf.zarr --format json
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect.py -i /tmp/imerg.zarr --max-values 0
```
