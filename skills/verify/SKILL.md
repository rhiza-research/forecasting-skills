---
name: verify
description: Forecast vs observation verification on a shared grid — hits (event classification), bias (forecast − obs), or MAE (|forecast − obs|). Writes a weather-skills Zarr for plotting. Coarsen --obs onto the forecast lat/lon grid first. Align time with step-to-time / aggregate-temporal first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# verify

Cell-by-cell forecast verification against observations. Choose a metric
with `--metric`:

| `--metric` | Output variable | Meaning |
| --- | --- | --- |
| `hits` (default) | `event_hit` | Event classification at `--threshold` |
| `bias` | `bias` | `forecast − observation` per cell |
| `mae` | `mae` | `\|forecast − observation\|` per cell |

### Hits (`--metric hits`)

An **event** is `--variable` ≥ `--threshold` (default `1`, in stored units):

| Value | Meaning |
| --- | --- |
| `1` (`hit`) | forecast and truth both ≥ threshold |
| `-1` (`disagree`) | one is ≥ threshold and the other is not |
| `0` (`below`) | both below the threshold |

NaNs in either input stay NaN. Ensemble `number` is averaged before
comparison. Inputs are inner-joined (overlapping coordinates only).

Plot hits with `plot` (discrete red / gray / green map). For a lead-week
grid of obs, forecast, and verification maps, use `plot-verify`.

## When to use

- Binary event verification (`--metric hits`) — rain ≥ 1 mm, temperature ≥
  35 °C, …
- Continuous error maps (`--metric bias` or `--metric mae`) for forecast
  skill assessment on a shared grid.

**Match obs to the forecast, not the reverse.** Coarsen `--obs` onto the
forecast's lat/lon spacing and offset. Do not `downscale` the forecast onto
the obs grid. Run `step-to-time` on a classic forecast first. For a precip
threshold in `mm`, run `aggregate-temporal` then `convert-to-totals` first.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast <forecast.zarr> --obs <truth.zarr> \
    [--metric hits|bias|mae] [--variable NAME] [--threshold 1] \
    -o <verify.zarr>
```

### Arguments

- `--forecast` — forecast Zarr (required).
- `--obs` — truth / observation Zarr (required). Must already be on the
  forecast's spatial resolution.
- `--metric` — `hits`, `bias`, or `mae` (default `hits`).
- `--variable`, `-v` — data variable in both inputs. Default: each input's
  first usable variable (names may differ).
- `--threshold` — event cutoff for `--metric hits` only (default `1`).
- `--output`, `-o` — output Zarr.

### Output

One verification data variable (`event_hit`, `bias`, or `mae`). Hits output
carries CF `flag_values` `-1, 0, 1` and `flag_meanings`
`disagree below hit`. A regional score is printed to stdout (hit rate for
hits; cos-lat weighted mean for bias/mae).

## Examples

```bash
# Event hits Zarr for re-plotting
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast /tmp/s2s_weekly.zarr --obs /tmp/chirps_weekly.zarr \
    --metric hits --variable precip --threshold 1 -o /tmp/hits.zarr
uv run skills/plot/scripts/plot.py -i /tmp/hits.zarr -o /tmp/hits.png \
    --title "Weekly rain ≥ 1 mm"

# Bias error field
uv run ${CLAUDE_SKILL_DIR}/scripts/verify.py \
    --forecast /tmp/s2s_weekly.zarr --obs /tmp/chirps_weekly.zarr \
    --metric bias --variable precip -o /tmp/bias.zarr
```
