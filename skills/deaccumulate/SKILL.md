---
name: deaccumulate
description: Convert a cumulative-since-init forecast variable (e.g. ECMWF S2S `tp`) along its `step` axis into per-step diffs, so each step value represents the period since the previous step rather than the accumulation since initialization.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.0"
---

# deaccumulate

Source-agnostic deaccumulation primitive. Some forecast variables are stored as
values accumulated from the forecast init time (precipitation, surface
radiation, evaporation, snow water equivalent). This skill turns those into
per-step increments via `arr[i+1] - arr[i]`, clipped at zero.

## When to use

- Any cumulative-since-init forecast variable that needs to be plotted,
  compared against per-period observations, or further aggregated into longer
  windows.
- Common examples: ECMWF S2S total precipitation (`tp`), surface net solar
  radiation, evaporation, SWE.

## Usage

```
uv run scripts/deaccumulate.py --input <in.zarr> --output <out.zarr> \
    [--variable NAME]
```

### Arguments
- `--input`, `-i` — input Zarr containing a forecast envelope with a `step` dim.
- `--output`, `-o` — output Zarr.
- `--variable`, `-v` — variable to deaccumulate. If omitted and the input has a
  single data variable, that one is used. If multiple data vars are present,
  `--variable` is required.

### Output

Same dims and coords as the input EXCEPT the `step` axis is one shorter: the
first input step is dropped, and the remaining step coord values are
preserved. Values are `arr[i+1] - arr[i]` clipped at zero. The variable's
attrs are preserved.

If the input lacks a `step` dim, or has multiple data vars and no
`--variable`, the skill exits with code 2 and a clear message.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `rhiza_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes, so a renamed-but-unchanged input still cache-hits and a
same-named-but-modified input correctly cache-misses; `version` is the
git sha of the script at run time, or `"unknown"` when not resolvable. The previously stamped scalars `rhiza_deaccumulated`
and `rhiza_inputs` are no longer written — they were collision-prone across
a chain and are recoverable from `rhiza_history`.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `anchor_end`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--anchor-end`). A consumer
reconstructing a `uv run scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Composition with aggregate-temporal

Per-step diffs are additive across consecutive intervals, so running
`aggregate-temporal --method sum` on a deaccumulated forecast produces
correct per-window totals (e.g. weekly or dekadal precipitation). Running
`aggregate-temporal --method sum` directly on an accumulated variable
double-counts the earlier steps and inflates totals.

`deaccumulate` is also useful standalone: per-period diffs can be plotted
directly, or compared to per-period observations (e.g. IMERG, CHIRPS, station
data) without an extra aggregation step.

## Examples

```bash
uv run scripts/deaccumulate.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_per_step.zarr \
    --variable tp
```
