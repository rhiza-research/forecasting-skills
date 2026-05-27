---
name: concat
description: Concatenate two or more Rhiza Envelope Zarr stores along a named dimension, optionally assigning coordinate values to the new axis. Use when combining ensemble members, stitching time windows, or merging per-country fetches into a single dataset.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.0"
---

# concat

Source-agnostic concatenation along a named dim. Inputs must share all other dims and variables. If the concat dim does not exist on the inputs, a new dim is created (use `--coords` to assign values).

## When to use

- Stitching per-country or per-date fetches into one Zarr.
- Joining two ensemble halves (cf and pf) along `number`.
- Glueing back two time windows after parallel processing.

## Usage

```
uv run scripts/concat.py -i a.zarr -i b.zarr [-i ...] --dim DIM --output <out.zarr> \
    [--coords V1,V2,...]
```

### Arguments
- `--input`, `-i` — input Zarr (pass once per input; order is preserved). At least two are required.
- `--dim` — dimension name to concatenate along.
- `--coords` — optional comma-separated coord values to assign to the new dim. Length must match number of inputs; only used when `--dim` does not already exist on inputs.
- `--output`, `-o` — output Zarr.

### Output

A single Zarr with the concat dim extended. Attrs from the first input are preserved.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. Because concat takes multiple
inputs, its entry's `input` field is a list of `{basename, hash}` dicts (one per
input, in the order given on the command line). The upstream
chain is taken from the first input's `rhiza_history` (matching the attr
passthrough already done on the dataset). If any other input has a non-empty
`rhiza_history` that disagrees with the first input's, a warning is written to
stderr; concat does not attempt to reconcile divergent provenance. `args` is
the argparse namespace minus the `--input`/`--output` path strings; `version`
is the `_RHIZA_SKILL_VERSION` constant in `scripts/concat.py`, kept in
lockstep with `metadata.version` in this SKILL.md by the CI version-bump
workflow. Each input's `hash` is a sha256 over its stored bytes, so
renamed-but-unchanged inputs still match and same-named-but-modified inputs
do not.

## Example

```bash
uv run scripts/concat.py -i /tmp/cf.zarr -i /tmp/pf.zarr --dim number --output /tmp/ens.zarr
```
