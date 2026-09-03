---
name: convert-to-totals
description: Convert rate variables to period totals by multiplying by stamped aggregation_period (pint). Use as a terminal step before plotting. Requires aggregate-temporal first. Works after select has collapsed the only time/step sample (spatial-only map). Refuses precip totals (would double-count). Default --min-coverage 1.0. Refuses overlapping intervals — run select on time/step first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# convert-to-totals

Terminal conversion: **rate × stamped `aggregation_period` → amount**.

Intended only before plot/export. Refuses precip totals (`cell_methods` with
`sum`, or amount units) — multiplying an amount by the period would
double-count.

## When to use

- After `aggregate-temporal` (which stamps `aggregation_period`,
  `aggregation_coverage`, and `cell_methods` with `mean`/`minimum`/`maximum`).
- When you need depth totals (`mm`) for display, not further rate math.
- Native-only cubes (`data_interval` or CF bounds, but no `aggregation_period`) will not
  convert — aggregate first.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py \
    -i <rates.zarr> -o <totals.zarr> \
    [--variable NAME ...] [--min-coverage 1.0] [--time-dim DIM]
```

### Arguments

- `--min-coverage` — drop intervals whose `aggregation_coverage` is below this
  (0–1). Default **1.0** (every native sample has finite data).
  `aggregate-temporal` keeps incomplete bins and only stamps coverage; this
  flag is what filters them. All-NaN unpublished forecast leads stamp
  coverage 0 and are dropped at the default. A 21-day bin that only has 90%
  of the expected finite slots fails at the default; pass `0.6` to allow it.
  If no intervals remain, the skill errors.
- `--variable`, `-v` — limit to named data vars (default: all).
- `--time-dim` — time/step dim for the overlap gate (default: CF time or `step`).

There is no `--aggregation-period` override. The period comes from the
stamped attr.

### Gate

When spacing can be inferred (≥ 2 points), requires sample spacing on the
time/step axis **≥** `aggregation_period`. End-over-end weekly means
(Δt = 7 day, period = 7 day) are allowed. Overlapping series (rolling
`--window`, or two 21-day intervals 10 days apart) are refused — **run
`select`** (`--dim time` or `--dim step`, by `--index` or `--value`) to keep
a non-overlapping subset, then convert-to-totals. A **single** time/step
point (one aggregated bin) is allowed — including after `select` has already
collapsed that dim away (spatial-only map). In that case conversion uses the
stamped `aggregation_period` alone; `--min-coverage` and the overlap gate
do not apply.

### Output metadata

- Amount `units` / precip `standard_name` when applicable.
- Precip `long_name` → `Total precipitation` (replaces product/rate names; the
  quantity is now a period total). Rate-like `GRIB_name` is rewritten the same way.
- `cell_methods` → `{dim}: sum`.
- `aggregation_coverage` removed (coverage gate already applied).
- `aggregation_period` **kept** so plotters can choose a period-aware precip
  color scale (sub-pentad vs 5+ day CHIRPS classes).
