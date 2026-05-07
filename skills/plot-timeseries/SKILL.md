---
name: plot-timeseries
description: Render a single PNG with one 1D trace per input Zarr overlaid on a shared time axis. Use when you want to compare a variable across multiple Rhiza Envelope Zarrs as line traces. Inputs whose variable still has non-time dims after selection must list those dims via repeated --reduce flags; no silent averaging.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# plot-timeseries

Source-agnostic multi-input timeseries plotting. Takes one or more Rhiza
Envelope Zarrs and draws each as a 1D line on a single set of axes against
its time/step coord. Each trace is labelled in the legend by the input
filename stem.

This is a first-pass timeseries skill: it plots data that is already 1D
(only a time-like dim left after picking `--variable`) or data the caller
has explicitly told it how to reduce to 1D via repeated `--reduce DIM`
flags. There is no silent averaging of unspecified dims, and no reference /
climatology overlay support.

For a single-input quick-look that averages across all non-time dims
without ceremony, use the `plot` skill with `--style timeseries`.

## When to use

- Comparing the same variable across two or more datasets (e.g. forecast vs.
  observation, or two forecast models) as line traces on one figure.
- Plotting a single dataset as a 1D timeseries when you want explicit
  control over which dims are reduced.

## Usage

```
uv run scripts/plot_timeseries.py -i <a.zarr> [-i <b.zarr> ...] --output <out.png> \
    [--variable NAME] [--time-dim DIM] [--reduce DIM ...] [--title TEXT]
```

### Arguments
- `--input`, `-i` — input Zarr; repeat the flag for each input. Order is
  preserved and controls the legend order.
- `--output`, `-o` — PNG output path.
- `--variable`, `-v` — variable name. Defaults to the first data variable of
  the first input. Must exist in every input.
- `--time-dim` — name of the time-like dim. When omitted, `time` is used if
  present, else `step`, else the cf-xarray-identified time axis.
- `--reduce` — name of a non-time dim to average out before plotting.
  Repeatable: pass once per dim to reduce. Required when an input's variable
  has any non-time dims after variable selection; the skill exits with an
  error rather than silently averaging.
- `--title` — optional figure title.

### Output

A PNG at `--output`, single axes (`figsize=(10, 6)`), one line per input,
legend on the axes. The y-axis label is `<variable>` plus `[<units>]` when
the variable carries a `units` attribute.

## Examples

Two forecast Zarrs, both already point-extracted (1D along `step`):

```bash
uv run scripts/plot_timeseries.py \
    -i /tmp/ecmwf_nairobi.zarr -i /tmp/ifs_nairobi.zarr \
    --variable tp --output /tmp/forecasts.png \
    --title "Nairobi precip forecast"
```

Two gridded Zarrs averaged over space and ensemble:

```bash
uv run scripts/plot_timeseries.py \
    -i /tmp/ecmwf_kenya.zarr -i /tmp/imerg_kenya.zarr \
    --variable tp \
    --reduce number --reduce latitude --reduce longitude \
    --output /tmp/precip_ts.png
```
