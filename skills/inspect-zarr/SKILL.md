---
name: inspect-zarr
description: Inspect a weather-skills standard dataset Zarr — print dimension sizes, coordinate values, and a data-variable summary (names, dims, dtype, units, min/max/mean, finite/NaN counts, truncated value sample). Use when you need to see what is in a Zarr before clipping, selecting, aggregating, or plotting, or to confirm values after a fetch. Data arrays can be huge: this skill never dumps them in full.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_zarr.py *)
metadata:
  catalog-group: agent-tooling
---

# inspect-zarr

Read-only dump of a Zarr store's structure **and** a bounded look at the
data. Prints dimension sizes, every coordinate (with values), and for each
data variable: dtype, shape, units, min/max/mean, finite vs NaN counts, and
a truncated sample of cells. It does not write an output store.

**Data arrays can be huge.** Stats scan the array (that can take time on a
large store) but stdout stays small: a handful of numbers plus a sample
capped at 256 cells. Never dump the arrays yourself — do not `print(ds)`,
open the Zarr in Python to ravel it, or pass `--max-values 0` expecting a
full data dump. `--max-values 0` still prints every *coordinate* value;
data samples stay capped. To look at a specific slice, `clip-region` /
`select` first, then inspect the smaller store.

Use `provenance` when you need the `weather_skills_history` lineage rather than
the grid itself. For a generated plot PNG, use `inspect-figure`.

## When to use

- Checking which dims and coordinates a fetch or transform produced.
- Reading lat/lon/time (or `step` / `number`) values before `select`,
  `clip-region`, `aggregate-temporal`, or `plot`.
- Confirming units on data variables after `unit-convert` or
  `convert-to-totals`, and whether `data_interval` / `aggregation_period` /
  `aggregation_coverage` are present.
- Checking whether a field is all NaN, all zero, or in a plausible range
  (especially after `inspect-figure` reports `BLANK`).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_zarr.py --input <in.zarr> \
    [--format human|json] [--max-values N]
```

### Arguments

- `--input`, `-i` — Zarr to inspect (`Dataset("any")`; any weather-skills
  store, including precip totals).
- `--format` — `human` (default) or `json`.
- `--max-values` — maximum coordinate values printed per coordinate, and
  the data-variable sample size (default 24). Long axes print a head/tail
  preview and the total count. `0` prints every coordinate value; data
  samples still cap at 256 cells.

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
    finite 24/24  min 1  max 1  mean 1
    sample: 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1
```

A truncated coordinate line ends with `(N values)`. A truncated data sample
ends with `(K of N cells)`.

**json** — the same information as `dims`, `coords` (`values` / `truncated` /
`size`), and `data_vars` (plus `n`, `n_finite`, `n_nan`, `min`, `max`,
`mean`, `sample`, `sample_truncated`). Non-finite numbers in JSON are
`null`. Use `provenance` for `weather_skills_history`.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_zarr.py -i /tmp/ecmwf.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_zarr.py -i /tmp/ecmwf.zarr --format json
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_zarr.py -i /tmp/imerg.zarr --max-values 0
```
