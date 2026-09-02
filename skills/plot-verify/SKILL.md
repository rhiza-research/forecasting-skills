---
name: plot-verify
description: Plot a lead-week verification grid from pre-computed verify Zarrs. Columns are week-4 through week-1 forecasts; rows are obs, forecast, and the verify metric map. Run the verify skill on each forecast/obs pair first. For precipitation, aggregate-temporal then convert-to-totals before verify. Use --fontsize to enlarge column/row labels, ticks, and colorbars (default 14).
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py *)
metadata:
  version: "0.0.3"
  catalog-group: figure
---

# plot-verify

Lead-week **verification figure** for **one observation week**. This skill
**plots only** — it does not compute verification. Run `verify` on each
forecast/obs pair first, then pass the resulting Zarrs here.

Columns run **least recent to most recent** (week-4 on the left, week-1
on the right):

| | Week 4 | Week 3 | Week 2 | Week 1 |
| --- | --- | --- | --- | --- |
| obs product | obs map | obs map | obs map | obs map |
| forecast product | week-4 map | week-3 map | week-2 map | week-1 map |
| Verification | verify map | verify map | verify map | verify map |

The verification row comes from `--verify` Zarrs (output of the `verify`
skill). All `--verify` inputs must share the same `verify_metric`
(`hits`, `bias`, or `mae`). Regional scores are read from each verify
Zarr's `verify_score_summary` attr (stamped by `verify`).

## Pipeline (one obs week)

1. Prepare obs and each lead's forecast (aggregate, coarsen obs onto
   forecast grid, select verifying week) — same as before.
2. For each lead, run `verify`:

```bash
uv run skills/verify/scripts/verify.py \
    --forecast /tmp/s2s_week4.zarr --obs /tmp/chirps_week.zarr \
    --metric hits --threshold 1 -o /tmp/verify_w4.zarr
# repeat for week 3, 2, 1 …
```

3. Pass obs, forecasts, and verify Zarrs to this skill (week-4 first).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs <obs.zarr> \
    --forecast <week4.zarr> --verify <verify_w4.zarr> \
    --forecast <week3.zarr> --verify <verify_w3.zarr> \
    ... \
    -o <out.png> [--variable NAME] \
    [--lead "Week 4" ...] [--title TEXT] [--fontsize N] [--colormap NAME] \
    [--bbox N/W/S/E] [--mask-geojson PATH]
```

### Arguments

- `--obs` — observation Zarr for the verifying week (required).
- `--forecast` — forecast Zarr for that week at one lead. Repeat with
  matching `--verify`.
- `--verify` — verify Zarr from the `verify` skill for that lead.
  **Required once per `--forecast`**, same order.
- `--variable`, `-v` — obs/forecast data variable (verify Zarrs carry
  their own verification variable).
- `--lead` — column title, once per `--forecast`.
- `--label` — row title override. Pass once for `--obs`, then once per
  `--forecast` (same order). The forecast row uses one label when all match,
  otherwise joins unique labels with ` / `. The verify row stays the metric
  name (Hits, Bias, MAE). When omitted, row titles are inferred from provenance.
- `--fontsize` — base font size for column/row labels, ticks, and colorbars
  (default 14). Raise on user request (e.g. `--fontsize 18`).
- `--colormap`, `--title`, `--bbox`, `--mask-geojson`, `--output` — as before.

### Output

A 3 × N PNG. Stdout echoes each column's `verify_score_summary` from
the corresponding `--verify` Zarr.

## Example

```bash
# Step 1: verify each lead (hits example)
for w in 4 3 2 1; do
  uv run skills/verify/scripts/verify.py \
    --forecast /tmp/s2s_week${w}.zarr --obs /tmp/chirps_week.zarr \
    --metric hits --threshold 1 -o /tmp/verify_w${w}.zarr
done

# Step 2: plot
uv run ${CLAUDE_SKILL_DIR}/scripts/plot_verify.py \
    --obs /tmp/chirps_week.zarr \
    --forecast /tmp/s2s_week4.zarr --verify /tmp/verify_w4.zarr \
    --forecast /tmp/s2s_week3.zarr --verify /tmp/verify_w3.zarr \
    --forecast /tmp/s2s_week2.zarr --verify /tmp/verify_w2.zarr \
    --forecast /tmp/s2s_week1.zarr --verify /tmp/verify_w1.zarr \
    --variable precip --bbox 5/34/-5/42 \
    --title "Kenya weekly precip verification" \
    -o /tmp/verify_week.png
```
