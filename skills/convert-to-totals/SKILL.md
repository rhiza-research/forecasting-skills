---
name: convert-to-totals
description: Convert rate variables to period totals by multiplying by stamped aggregation_period (pint). Use as a terminal step before plotting; do not feed totals back into rate-math skills. Requires aggregate-temporal first. Default --min-coverage 1.0. Refuses overlapping intervals — run select on time/step first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py *)
metadata:
  catalog-group: transforms
---

# convert-to-totals

Terminal conversion: **rate × stamped `aggregation_period` → amount**.

Intended only before plot/export. Rate-path skills refuse precip totals
(`cell_methods` with `sum`, or amount units) on input.

## When to use

- After `aggregate-temporal` (which stamps `aggregation_period`,
  `aggregation_coverage`, and `cell_methods` with `mean`/`minimum`/`maximum`).
- When you need depth totals (`mm`) for display, not further rate math.
- Native-only cubes (`data_interval` but no `aggregation_period`) will not
  convert — aggregate first.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py \
    -i <rates.zarr> -o <totals.zarr> \
    [--variable NAME ...] [--min-coverage 1.0] [--time-dim DIM]
```

### Arguments

- `--min-coverage` — drop intervals whose `aggregation_coverage` is below this
  (0–1). Default **1.0** (every native sample present). A 21-day bin that
  only has 90% of the expected slots fails at the default; pass `0.6` to
  allow it. If no intervals remain, the skill errors.
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
point (one aggregated bin) is allowed.

### Output metadata

- Amount `units` / precip `standard_name` when applicable.
- `cell_methods` → `{dim}: sum`.
- `aggregation_period` and `aggregation_coverage` removed.
