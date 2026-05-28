---
name: aggregate-temporal
description: Roll up a Rhiza Envelope Zarr along its time axis (or forecast step axis) into fixed windows (daily, weekly, dekadal, monthly) with a chosen reducer. Use whenever any dataset needs to be resampled to a canonical aggregation period before plotting or comparison.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.2"
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
    [--time-dim DIM] [--anchor-end YYYY-MM-DD]
```

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr.
- `--period` — window size: `daily` (1d), `weekly` (7d), `dekadal` (10d), `monthly` (calendar month for the default forward-anchored resample; 30-day approximation when combined with `--anchor-end`).
- `--method` — reducer: `sum` (default for totals), `mean`, `max`, `min`.
- `--time-dim` — override; by default uses `time` if present, else `step`.
- `--anchor-end` — ISO date (`YYYY-MM-DD`) used to anchor the LAST bin
  on the obs/time-resample path (no effect on the forecast `step`
  path). See "Anchor end" below.

### Method and intensive quantities

`--method sum` adds the values within each window into a period total, which is
meaningful only for an extensive quantity (an amount that accumulates, e.g.
precipitation depth in `mm` or `kg m**-2`). When `--method sum` is requested on a
variable that is clearly an intensive quantity, the skill exits with status 2
before any output is written and names the variable and the signal that marks it
as intensive. Detection is conservative — it fires only on high-confidence
signals and leaves ambiguous metadata to proceed:

- a `standard_name` of `air_temperature` or any name ending in `_temperature`;
- temperature units (`K`, `degK`, `degC`, `Celsius`, `degree_Celsius`, `°C`);
- pressure units (`Pa`, `hPa`, `mbar`, `bar`) when the `standard_name` also
  indicates pressure;
- a percentage (`%`, `percent`).

The other reducers (`mean`, `max`, `min`) are always accepted, and precipitation
(`mm`, `kg m**-2`) with `--method sum` proceeds.

### Output

Same variables; the time/step axis is replaced by the aggregated window.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `rhiza_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes, so a renamed-but-unchanged input still cache-hits and a
same-named-but-modified input correctly cache-misses; `version` is the
`_RHIZA_SKILL_VERSION` constant in `scripts/aggregate.py`, kept in lockstep
with `metadata.version` in this SKILL.md by the CI version-bump workflow.
Cache-hit comparison reads the existing output's
`rhiza_history`: a hit requires the upstream entries to match and the last
entry's `skill`, `args`, and `input` to match the proposed new entry.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `anchor_end`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--anchor-end`). A consumer
reconstructing a `uv run scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

### Step coordinate convention

For forecast (`step`) inputs, the output `step` coord is the **right edge** of
each aggregation bucket — i.e. each value is labeled with the end of the
period it covers. Buckets are **left-open and right-closed** (`(left, right]`),
so a step value sitting on a period boundary (e.g. `step=7d` for end-of-period
labeled data like a deaccumulated forecast) lands in the bucket it physically
belongs to. Trailing partial buckets that would extend past the input's last
step are dropped rather than synthesized.

### Anchor end

By default, the obs/time path delegates to `xr.resample`, which anchors
bins forward from the start of the input time axis. Pass
`--anchor-end YYYY-MM-DD` to instead anchor the LAST bin to end on that
date: bins are `period`-day windows (`(left, right]`) synthesized
backward from `--anchor-end` while their `left` edge is `>=` the input's
earliest timestamp. Partial bins at the start whose `left` falls before
the input range are dropped. The output time coord for each bin is the
bin's right edge (matching the right-edge convention used for
forecast `step` aggregation), so the last bin's label is exactly
`--anchor-end`.

Caveat for `monthly`: with `--anchor-end`, monthly bins are 30-day
fixed-width windows, not calendar months. Without `--anchor-end`,
`monthly` continues to mean calendar months (`xr.resample("MS")`).

Example — anchor the last weekly bin to end on 2026-05-12:

```bash
uv run scripts/aggregate.py -i /tmp/imerg.zarr -o /tmp/imerg_weekly.zarr \
    --period weekly --method sum --anchor-end 2026-05-12
```

### Cumulative-since-init variables

For variables that are stored as cumulative-since-init (e.g. ECMWF S2S `tp`),
run the `deaccumulate` skill before `aggregate-temporal` so each step value
is per-period rather than cumulative. Running `--method sum` directly on an
accumulated variable double-counts earlier steps and inflates totals.

## Examples

```bash
uv run scripts/aggregate.py -i /tmp/imerg.zarr -o /tmp/imerg_dekadal.zarr \
    --period dekadal --method sum
```

```bash
uv run scripts/aggregate.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_weekly.zarr \
    --period weekly --method sum
```
