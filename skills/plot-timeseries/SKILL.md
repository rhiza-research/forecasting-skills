---
name: plot-timeseries
description: Render a single PNG with one 1D series per input Zarr overlaid on a shared time axis, as lines (default) or grouped bars. Repeatable --trace SELECTOR:k=v styles one series (color, linewidth, marker, zorder) by 1-based input index, legend label, or a unique token in the label (e.g. 2026). Use when you want to compare a variable across multiple weather-skills standard dataset Zarrs. Inputs whose variable still has non-time dims after selection must list those dims via repeated --reduce flags; no silent averaging. For precipitation, run aggregate-temporal then convert-to-totals first — plot totals (`mm`), not rates.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py *)
metadata:
  version: "0.0.2"
  catalog-group: figure
---

# plot-timeseries

Source-agnostic multi-input timeseries plotting. Takes one or more weather-skills
standard dataset Zarrs and draws each as a 1D series on a single set of axes against
its time/step coord. `--style line` (default) is a polyline with a marker at each
time; `--style bar` is a grouped bar chart (one bar group per time, one bar per
input). Each series is labeled in the legend by a size-1
`station_id` / `point_id` (plus `name` when present), else the input
filename stem, else `weather_skills_source`.

It plots data that is already 1D (only a time-like dim left after picking
`--variable`) or data the caller has explicitly told it how to reduce to 1D
via repeated `--reduce DIM` flags. There is no silent averaging of
unspecified dims, and no reference / climatology overlay support.

A forecast input whose axis is `step` (timedelta lead times) plus a scalar
init `time` is plotted against **valid time** (`init + step`) so the x-axis
shows calendar dates, not raw nanoseconds. Run `step-to-time` first if you
need a real `time` dim for other skills (difference, plot-compare).

For a single-input quick-look, use the `plot` skill with
`--style timeseries`, which averages across all non-time dims by default
(no `--reduce` flags needed).

## When to use

- Comparing the same variable across two or more datasets (e.g. forecast vs.
  observation, or two forecast models) as line traces or grouped bars on one
  figure.
- Highlighting one input among analog years (`--trace 2026:color=black,linewidth=2.5`).
- Plotting a single dataset as a 1D timeseries when you want explicit
  control over which dims are reduced. Period totals (dekadal/monthly precip)
  often read better as `--style bar`.

For maps of N forecasts (or forecasts vs gridded obs) over time, use
`plot-compare-forecasts`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py -i <a.zarr> [-i <b.zarr> ...] --output <out.png> \
    [--variable NAME] [--time-dim DIM] [--reduce DIM ...] [--title TEXT] \
    [--style line|bar] [--align-day-of-year] [--trace SELECTOR:k=v ...]
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
- `--style` — `line` (default) or `bar`. `bar` draws grouped bars (one group
  per time step; one bar per `--input`, offset within the group). Bar width is
  80% of the median time spacing, split across inputs. Single-input `bar` is
  just one bar per time.
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
- `--trace` — repeatable per-series style `SELECTOR:k=v[,k=v...]`. Unstyled
  series keep the matplotlib color cycle in `--input` order. Selectors:
  - `*` — all series (applied first; later `--trace` flags override)
  - a 1-based `--input` index (`4` is the fourth `-i`; only `1…N` count as
    indices, so `2026` is a year token, not input 2026)
  - the legend label (filename stem, or `station_id`)
  - a unique alphanumeric token in that label (`2026` matches `chirps_2026`)
  Keys: `color` (matplotlib name, hex, or grayscale `0-1`), `linewidth` /
  `lw`, `linestyle` / `ls`, `marker`, `markersize` / `ms`, `alpha`,
  `zorder`. Line-only keys (`linewidth`, `linestyle`, `marker`, `markersize`)
  error with `--style bar`. Quote hex colors (`--trace '2026:color=#222'`).
  An unmatched or ambiguous selector exits 2.

### Output

A PNG at `--output`, single axes (`figsize=(10, 6)`), one series per input
(line with markers, or bars), legend on the axes. The y-axis label is the variable `long_name` (then
`GRIB_name`, then the variable name) plus `[<units>]` when the variable
carries a `units` attribute. Units are a short display form (`mm/day`,
`°C`), not the on-disk CF string.

### Input units

All traces share one y-axis whose label takes the units of the first input.
When the overlaid inputs carry the plotted variable in differing `units`, series
in different units are drawn against a single scale and labeled with only one of
them. The skill prints a warning to stderr naming the distinct units and still
renders the figure (exit status 0); it is a rendering caveat, not a hard error.
Only inputs that carry a `units` attr participate in the comparison.

### Provenance

The decorator stamps a single `weather_skills_history` JSON array into the PNG
metadata. Read-back:

```bash
python3 -c "from PIL import Image; import json; img = Image.open('out.png'); print(json.loads(img.info['weather_skills_history']))"
```

## Examples

Two forecast Zarrs, both already point-extracted (1D along `step`):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ecmwf_nairobi.zarr -i /tmp/ifs_nairobi.zarr \
    --variable tp --output /tmp/forecasts.png \
    --title "Nairobi precip forecast"
```

Two gridded Zarrs averaged over space and ensemble:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ecmwf_kenya.zarr -i /tmp/imerg_kenya.zarr \
    --variable tp \
    --reduce number --reduce latitude --reduce longitude \
    --output /tmp/precip_ts.png
```

Period totals as grouped bars (forecast vs observations):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/ecmwf_weekly.zarr -i /tmp/imerg_weekly.zarr \
    --variable tp --style bar \
    --reduce latitude --reduce longitude \
    --output /tmp/precip_bars.png \
    --title "Weekly precip totals"
```

Analog years in gray, current year emphasized (token match on the stem):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_timeseries.py \
    -i /tmp/chirps_2006.zarr -i /tmp/chirps_2015.zarr -i /tmp/chirps_2026.zarr \
    --align-day-of-year --reduce latitude --reduce longitude \
    --trace '*:color=0.65,linewidth=1.2' \
    --trace '2026:color=black,linewidth=2.5,zorder=5,markersize=7' \
    --output /tmp/analogs.png \
    --title "Kenya OND analog rainfall"
```
