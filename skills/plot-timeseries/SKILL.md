---
name: plot-timeseries
description: Render a single PNG with one 1D trace per input Zarr overlaid on a shared time axis. Use when you want to compare a variable across multiple weather-skills envelope Zarrs as line traces. Inputs whose variable still has non-time dims after selection must list those dims via repeated --reduce flags; no silent averaging.
license: MIT
compatibility: Requires Python 3.12 and uv.
metadata:
  version: "0.1.11"
  catalog-group: visualization
---

# plot-timeseries

Source-agnostic multi-input timeseries plotting. Takes one or more weather-skills
envelope Zarrs and draws each as a 1D line on a single set of axes against
its time/step coord. Each trace is labeled in the legend by the input
filename stem.

It plots data that is already 1D (only a time-like dim left after picking
`--variable`) or data the caller has explicitly told it how to reduce to 1D
via repeated `--reduce DIM` flags. There is no silent averaging of
unspecified dims, and no reference / climatology overlay support.

For a single-input quick-look, use the `plot` skill with
`--style timeseries`, which averages across all non-time dims by default
(no `--reduce` flags needed).

## When to use

- Comparing the same variable across two or more datasets (e.g. forecast vs.
  observation, or two forecast models) as line traces on one figure.
- Plotting a single dataset as a 1D timeseries when you want explicit
  control over which dims are reduced.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py -i <a.zarr> [-i <b.zarr> ...] --output <out.png> \
    [--variable NAME] [--time-dim DIM] [--reduce DIM ...] [--title TEXT] [--align-day-of-year]
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
- `--align-day-of-year` — opt-in (default off). Plot each trace against its
  day-of-year (1–366) instead of its absolute date, so inputs from different
  years overlay on a shared x-axis; the x-axis label becomes `day of year`.
  Caveats:
  - Requires a calendar-date time axis. It errors (exit 2) on a non-date axis,
    such as a forecast `step` timedelta; drop the flag or select a date dim
    with `--time-dim`.
  - Intended for within-year seasons. A season that crosses the calendar-year
    boundary (e.g. Dec–Feb) wraps at the year boundary (day 366 → 1 in leap
    years, 365 → 1 otherwise) and will not align as one contiguous block; the
    skill prints a stderr warning and still renders.
  - The 1–366 range assumes a standard calendar; model calendars yield their
    own range (e.g. a `360_day` calendar yields 1–360), so overlaying inputs
    on different calendars can misalign by several days without error.
  - Leap vs non-leap years offset day-of-year by ~1 after Feb 29, so dates
    after February in a leap year land one day-of-year higher than the same
    date in a non-leap year.

### Output

A PNG at `--output`, single axes (`figsize=(10, 6)`), one line per input,
legend on the axes. The y-axis label is `<variable>` plus `[<units>]` when
the variable carries a `units` attribute.

### Input units

All traces share one y-axis whose label takes the units of the first input.
When the overlaid inputs carry the plotted variable in differing `units`, lines
in different units are drawn against a single scale and labeled with only one of
them. The skill prints a warning to stderr naming the distinct units and still
renders the figure (exit status 0); it is a rendering caveat, not a hard error.
Only inputs that carry a `units` attr participate in the comparison.

### Provenance

Every PNG carries a per-input `tEXt` chunk plus a producer key:

- `weather_skills_history_a`, `weather_skills_history_b`, `weather_skills_history_c`, ... — one key per
  `-i` input, lettered by CLI position (first input → `_a`, second → `_b`,
  third → `_c`, ...). Each value is a JSON-encoded array of
  `{skill, version, args, input}` entries with the same schema used for the
  zarr `weather_skills_history` attribute. The last entry records this
  `plot-timeseries` invocation, with `input` set to that branch's
  `{basename, hash}`. Preceding entries are the upstream chain inherited
  from that input's `weather_skills_history` (empty if the input had none — a stderr
  warning is emitted and the array contains only the rendering entry).
  Inputs beyond 26 are rejected at argument-validation time, before any
  rendering.
- `Software` — set to `forecasting-skills` so generic image tools like
  `exiftool` surface the producer prominently.

Read-back:

```bash
python3 -c "from PIL import Image; import json; img = Image.open('out.png'); print(json.loads(img.info['weather_skills_history_a']))"
```

## Examples

Two forecast Zarrs, both already point-extracted (1D along `step`):

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ecmwf_nairobi.zarr -i /tmp/ifs_nairobi.zarr \
    --variable tp --output /tmp/forecasts.png \
    --title "Nairobi precip forecast"
```

Two gridded Zarrs averaged over space and ensemble:

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ecmwf_kenya.zarr -i /tmp/imerg_kenya.zarr \
    --variable tp \
    --reduce number --reduce latitude --reduce longitude \
    --output /tmp/precip_ts.png
```
