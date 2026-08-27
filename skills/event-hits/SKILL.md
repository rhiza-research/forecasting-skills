---
name: event-hits
description: Classify event hits and misses between a forecast Zarr and a truth Zarr. An event is the named variable at or above --threshold (default 1). A hit is both above; a disagree is when they differ; both below is below. Writes a weather-skills Zarr for plotting. Coarsen --obs onto the forecast lat/lon grid (match obs to the forecast resolution, do not downscale the forecast). Align time with step-to-time / aggregate-temporal first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/event_hits.py *)
metadata:
  catalog-group: transforms
---

# event-hits

Cell-by-cell event verification. An **event** is `--variable` ≥ `--threshold`
(default `1`, in the stored units). Each cell is then:

| Value | Meaning |
| --- | --- |
| `1` (`hit`) | forecast and truth both ≥ threshold |
| `-1` (`disagree`) | one is ≥ threshold and the other is not |
| `0` (`below`) | both below the threshold |

NaNs in either input stay NaN. Ensemble `number` is averaged before the
threshold. Inputs are inner-joined (overlapping coordinates only).

Plot the output with `plot`; CF `flag_values` draw as a discrete
red / gray / green map (disagree / below / hit). For one obs week against
week-4 through week-1 forecasts (obs, forecast values, and hits in one grid), use
`plot-verify`.

## When to use

- Binary event verification of a forecast against observations on a shared
  grid and time axis (rain ≥ 1 mm, temperature ≥ 35 °C, …).
- A map of where both saw the event, where they disagreed, and where both
  were below the cutoff.

**Match obs to the forecast, not the reverse.** Coarsen `--obs` onto the
forecast's lat/lon spacing and offset (`coarsen --target-resolution` /
`--offset` from the forecast grid). Do not `downscale` the forecast onto
the obs grid. Run `step-to-time` on a classic forecast first. If the
variable names differ (`tp` vs `precip`), `rename` one of them and pass
`-v`. For a precip threshold in `mm`, run `aggregate-temporal` then
`convert-to-totals` first.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/event_hits.py \
    --forecast <forecast.zarr> --obs <truth.zarr> \
    [--variable NAME] [--threshold 1] -o <hits.zarr>
```

### Arguments

- `--forecast` — forecast Zarr (required).
- `--obs` — truth / observation Zarr (required). Must already be on the
  forecast's spatial resolution (coarsen obs to the forecast grid first).
- `--variable`, `-v` — data variable that must exist in both inputs.
  Default: each input's first usable variable (names may differ).
- `--threshold` — event cutoff: value ≥ this counts as the event
  (default `1`, in the stored units).
- `--output`, `-o` — output Zarr.

### Output

One data variable `event_hit` (`float32`) with CF `flag_values`
`-1, 0, 1` and `flag_meanings` `disagree below hit`. `event_threshold`
and `event_variable` record the cutoff and variable used.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/event_hits.py \
    --forecast /tmp/s2s_weekly.zarr --obs /tmp/chirps_weekly.zarr \
    --variable precip --threshold 1 -o /tmp/event_hit.zarr
uv run skills/plot/scripts/plot.py -i /tmp/event_hit.zarr -o /tmp/event_hit.png \
    --title "Weekly rain ≥ 1 mm"
```
