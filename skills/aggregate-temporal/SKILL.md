---
name: aggregate-temporal
description: Roll up a Rhiza Envelope Zarr along its time axis (or forecast step axis) into fixed windows (daily, weekly, dekadal, monthly) with a chosen reducer. Use whenever any dataset needs to be resampled to a canonical aggregation period before plotting or comparison.
compatibility: Requires Python 3.10+ and uv.
---

# aggregate-temporal

Source-agnostic temporal aggregation. Works on:
- Observation envelopes with a `time` dim (e.g. CHIRPS, IMERG, TAHMO).
- Forecast envelopes with a `step` dim (e.g. ECMWF S2S).

Autodetects which dim is present. For forecasts, aggregates ensemble members (`number`) independently.

## When to use

- Turning daily/half-hourly observations into weekly or dekadal totals.
- Selecting weekly or dekadal subsets of a forecast initialized at multiple steps.

## Usage

```
uv run scripts/aggregate.py --input <in.zarr> --output <out.zarr> \
    --period daily|weekly|dekadal|monthly [--method sum|mean|max|min] \
    [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr.
- `--period` — window size: `daily` (1d), `weekly` (7d), `dekadal` (10d), `monthly` (calendar month).
- `--method` — reducer: `sum` (default for totals), `mean`, `max`, `min`.
- `--time-dim` — override; by default uses `time` if present, else `step`.

### Output

Same variables; the time/step axis is replaced by the aggregated window. `rhiza_aggregation` attr is stamped (e.g. `weekly-sum`).

## Examples

```bash
uv run scripts/aggregate.py -i /tmp/imerg.zarr -o /tmp/imerg_dekadal.zarr \
    --period dekadal --method sum
```

```bash
uv run scripts/aggregate.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_weekly.zarr \
    --period weekly --method sum
```
