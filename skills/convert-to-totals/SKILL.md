---
name: convert-to-totals
description: Convert rate variables to period totals by multiplying by aggregation_period (pint). Use as a terminal step before plotting; do not feed totals back into rate-math skills. Refuses when the time axis spacing is finer than aggregation_period (rolling windows).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py *)
metadata:
  catalog-group: transforms
---

# convert-to-totals

Terminal conversion: **rate × `aggregation_period` → amount**.

Intended only before plot/export. Rate-path skills refuse precip totals
(`cell_methods` with `sum`, or amount units) on input.

## When to use

- After `aggregate-temporal` (which stamps `aggregation_period` and
  `cell_methods` with `mean`/`minimum`/`maximum`).
- When you need depth totals (`mm`) for display, not further rate math.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/convert_to_totals.py \
    -i <rates.zarr> -o <totals.zarr> \
    [--variable NAME ...] [--aggregation-period '7 day'] [--time-dim DIM]
```

### Arguments

- `--aggregation-period` — override per-var `aggregation_period` attr (pint
  duration, e.g. `7 day`, `1 dekad`).
- `--variable`, `-v` — limit to named data vars (default: all).
- `--time-dim` — time/step dim for the timestep gate (default: CF time or `step`).

### Gate

Requires sample spacing on the time/step axis **≥** `aggregation_period`.
End-over-end weekly means (Δt = 7 day, period = 7 day) are allowed. Daily
series of rolling 7-day means (Δt = 1 day, period = 7 day) are refused.

### Output metadata

- Amount `units` / precip `standard_name` when applicable.
- `cell_methods` → `{dim}: sum`.
- `aggregation_period` removed.
