---
name: standardize-anomaly
description: Compute a standardized anomaly (z-score) — (ds[var] - clim[var_avg]) / clim[var_std] — for one or more --variable names, given a data Zarr and a climatology Zarr (e.g. from clim-fetch) with matching dims. Dimensionless output, refuses (does not just warn) on units mismatch between the field and its climatology. Use for anomaly detection, SPI/SPEI-style standardized indices, or comparing a value's unusualness across variables/locations on the same scale. Not for a plain physical-unit anomaly (field minus mean, no division) — use difference for that.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py *)
metadata:
  catalog-group: transforms
---

# standardize-anomaly

Computes a **standardized anomaly** — the z-score of a field against a
climatology's mean and spread — not a plain physical-unit anomaly. The two
are genuinely different quantities:

- **Anomaly**: `field − mean`. Still in the field's original units (a
  temperature anomaly in °C, a precip anomaly in mm/day). Already covered by
  `difference` (subtract a climatology mean from a field).
- **Standardized anomaly / z-score**: `(field − mean) / std`. Dimensionless.
  The basis for indices like SPI/SPEI. What this skill computes.

For each `--variable NAME`, reads `NAME` from `--input` and `NAME_avg` /
`NAME_std` from `--climatology` (e.g. `clim-fetch`'s own output naming),
aligns them (xarray inner join on shared dims, same as `difference`), and
writes `NAME_anomaly`.

## When to use

- Anomaly detection: how unusual is today's value relative to this
  day-of-year's typical mean and spread?
- Standardized indices (SPI/SPEI-style): needs the climatology's mean *and*
  std, not mean alone.
- Comparing unusualness across variables or locations on one common,
  dimensionless scale (a z-score of +2 means the same thing for precip in
  Nairobi as for temperature in Lagos; a plain anomaly in mm/day and one in
  °C do not compare at all).

Not for a plain physical-unit anomaly — use `difference` with a mean-only
baseline for that; no division, output keeps the field's original units.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py \
  --input <ds.zarr> --climatology <clim.zarr> --variable NAME [--variable NAME ...] \
  --output <out.zarr>
```

### Arguments

- `--input`, `-i` — the data Zarr (e.g. an observation or forecast field).
- `--climatology` — the climatology Zarr, carrying `NAME_avg`/`NAME_std` for
  every `--variable` requested (e.g. `clim-fetch`'s output).
- `--variable`, `-v` — repeatable, **required** (no default — unlike
  `difference`, there's no "variables shared by both inputs" to fall back to,
  since the climatology's own variable names are `NAME_avg`/`NAME_std`, not
  `NAME`). Each name must be a data variable of `--input`, and both
  `NAME_avg` and `NAME_std` must be data variables of `--climatology`. All
  requested variables are checked against both inputs up front, in one pass —
  a violation on any of them exits non-zero listing everything missing from
  each side at once, not just the first problem found.
- `--output`, `-o` — output Zarr.

### Alignment

Same as `difference`: an xarray-aligned inner join on shared dims (e.g.
`time`, `lat`, `lon`), broadcasting over dims present on only one side. A
`--climatology` at a coarser resolution than `--input` (e.g. `clim-fetch
--window 7`) still works — the climatology's fewer, coarser-grained days just
align to whichever `--input` days share the exact same coordinate value.

### Units

Both sides are pint-quantified on open, so unit compatibility is checked by
pint itself, not by comparing `units` strings: dimensionally compatible but
differently-scaled units (`mm/day` vs `cm/day`) are converted and combined
correctly, automatically. A genuine mismatch (e.g. `mm/day` vs
`degree_Celsius`) **errors** — this is stricter than `difference`, which only
warns on a `units` mismatch and proceeds. The reason: a difference with
mismatched units is wrong-scale but still a coherent number; a z-score built
from mismatched units isn't a valid ratio at all, so proceeding would produce
a number that looks fine but means nothing.

### Output

One `NAME_anomaly` variable per `--variable`, dimensionless (`units: "1"`),
`long_name` set to `"NAME standardized anomaly"`. The field's own `units`
and other attrs are **not** copied — unlike `difference`, which keeps the
first input's attrs (appropriate there since a difference of two rates is
still a rate; a z-score isn't a rate at all, so nothing physical carries
over).

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr, same shape as
`difference`'s: entry `input` is a list with one item per input (`--input`
then `--climatology`), each carrying that input's own upstream chain.
Inspect with the `provenance` skill.

## Example

```bash
# Standardize today's IMERG precip against its day-of-year climatology.
uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py \
  --input /tmp/imerg_today.zarr --climatology /tmp/imerg_clim.zarr \
  --variable precip --output /tmp/precip_zscore.zarr

# Two variables in one call.
uv run ${CLAUDE_SKILL_DIR}/scripts/standardize_anomaly.py \
  --input /tmp/era5_today.zarr --climatology /tmp/era5_clim.zarr \
  --variable precip --variable t2m --output /tmp/zscores.zarr
```
