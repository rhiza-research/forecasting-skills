---
name: deaccumulate
description: Convert a leftover cumulative-since-init forecast variable along its `step` axis into per-step rates (precip → mm day-1). Fetchers (`ecmwf-fetch`, `dynamical-fetch`, `kenya-forecast-fetch`) already write rates — do not run this after them. Use on older cumulative archives that still have amount units.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# deaccumulate

Source-agnostic deaccumulation primitive. Some forecast variables are stored as
values accumulated from the forecast init time (precipitation, surface
radiation, evaporation, snow water equivalent). This skill turns those into
per-step increments via `arr[i+1] - arr[i]`, clipped at zero.

## When to use

- A forecast cube that is still cumulative since init (amount units such as
  `mm` / `kg m-2`, growing along `step`) — typically a leftover archive, not
  a current fetcher output.
- Other cumulative-since-init forecast fields (surface radiation, evaporation,
  SWE) that grow along `step`.

## When not to use

- **Current fetcher outputs** (`ecmwf-fetch` `tp`, `dynamical-fetch`
  `precipitation_surface`, `kenya-forecast-fetch` precip) — already rates
  (`mm day-1`). The skill refuses them. For period amounts, run
  `aggregate-temporal` then `convert-to-totals`.
- CHIRPS, IMERG, station precip, and any variable whose `units` are per-time
  (`mm day-1`, `kg m-2 s-1`) or whose `standard_name` ends in `_rate` / `_flux`.

## Input precondition

The input variable must be a cumulative-since-init accumulated quantity — a
depth or amount that grows along `step` (units such as `kg m**-2`, `m`, `mm`,
or `J m**-2`, and no rate/flux `standard_name`). The skill validates this from
the variable's metadata before differencing:

- It rejects (exit 2, with a message naming the variable and the offending
  metadata) inputs whose `units` carry a per-time denominator (e.g. `mm/day`,
  `kg m-2 s-1`, `m s-1`) or whose `standard_name` ends in `_rate` or `_flux`
  (e.g. `lwe_precipitation_rate`, `precipitation_flux`). Such inputs are
  per-time rates and are already per-period; differencing them produces
  meaningless values. A daily `mm/day` product (e.g. CHIRPS or IMERG) must not
  be deaccumulated. Same for dynamical.org `precipitation_surface`.
- When the variable has neither `units` nor `standard_name`, the skill cannot
  validate the input. It prints a stderr warning and proceeds, so an
  accumulated input that lacks a `units` attr still works.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py --input <in.zarr> --output <out.zarr> \
    [--variable NAME]
```

### Arguments
- `--input`, `-i` — input Zarr containing a forecast dataset with a `step` dim.
- `--output`, `-o` — output Zarr.
- `--variable`, `-v` — variable to deaccumulate. If omitted and the input has a
  single data variable, that one is used. If multiple data vars are present,
  `--variable` is required.

### Output

Same dims and coords as the input EXCEPT the `step` axis is one shorter: the
last input step is dropped, and remaining cells are labeled at each
interval's **left** edge (`step = 0` is the first period). Values are
`arr[i+1] - arr[i]` clipped at zero. For precip amount
inputs, the increment is divided by the step interval and stamped as
`mm day-1` (`lwe_precipitation_rate`). Other variables keep their input attrs.

If the input lacks a `step` dim, or has multiple data vars and no
`--variable`, the skill exits with code 2 and a clear message.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `weather_skills_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes, so a renamed-but-unchanged input still cache-hits and a
same-named-but-modified input correctly cache-misses; `version` is the
`_SKILL_VERSION` constant in `scripts/deaccumulate.py`.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `end_time`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--end-time`). A consumer
reconstructing a `uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Composition with aggregate-temporal

Deaccumulation turns cumulative precip into **per-step rates** (`mm day-1`).
Aggregate those rates with `aggregate-temporal --method mean` (or min/max),
then run `convert-to-totals` when you need period amounts for plotting.
Do not aggregate a still-accumulated variable — deaccumulate first.
Do not deaccumulate a current fetcher precip cube — it is already a rate;
aggregate those rates, then `convert-to-totals` if you need `mm`.

`deaccumulate` is also useful standalone: per-step rates can be compared to
daily rate observations (e.g. IMERG, CHIRPS, station data) without further
aggregation.

## Examples

```bash
# Leftover cumulative-since-init cube (current fetchers already write rates)
uv run ${CLAUDE_SKILL_DIR}/scripts/deaccumulate.py -i /tmp/cumulative_tp.zarr -o /tmp/tp_rates.zarr \
    --variable tp
```
