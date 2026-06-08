---
name: concat
description: Concatenate two or more Rhiza Envelope Zarr stores along a named dimension, optionally assigning coordinate values to the new axis. Use when combining ensemble members, stitching time windows, or merging per-country fetches into a single dataset.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.4"
---

# concat

Source-agnostic concatenation along a named dim. Inputs must share all other dims and variables. If the concat dim does not exist on the inputs, a new dim is created (use `--coords` to assign values).

## When to use

- Stitching per-country or per-date fetches into one Zarr.
- Joining two ensemble halves (cf and pf) along `number`.
- Glueing back two time windows after parallel processing.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i a.zarr -i b.zarr [-i ...] --dim DIM --output <out.zarr> \
    [--coords V1,V2,...]
```

### Arguments
- `--input`, `-i` — input Zarr (pass once per input; order is preserved). At least two are required.
- `--dim` — dimension name to concatenate along.
- `--coords` — optional comma-separated coord values to assign to the new dim. Length must match number of inputs; only used when `--dim` does not already exist on inputs.
- `--output`, `-o` — output Zarr.

### Input units

Concatenation combines the inputs into one array that carries a single `units`
label (the first input's attrs are preserved on the output). For each data
variable common to every input, the `units` attr is compared across the inputs
before any output is written. When two inputs carry the variable in differing
units, the skill exits with status 2 and names each input and its units; no
output is produced. Inputs that omit `units` for a variable are not treated as a
violation — only present values are compared.

### Output

A single Zarr with the concat dim extended. Attrs from the first input are preserved.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. Because concat takes multiple
inputs, its entry's `input` field is a list of `{basename, hash, history}` dicts
(one per input, in the order given on the command line). Each item's `history`
holds that input's full `rhiza_history` chain (an empty list when the input had
no `rhiza_history`), so the concat entry records every input branch and the
output is fully reproducible from its own provenance. The output's top-level
`rhiza_history` is a single linear array: the first input's chain followed by
this concat entry, matching the attr passthrough already done on the dataset.
`args` is the argparse namespace minus the `--input`/`--output` path strings;
`version` is the `_RHIZA_SKILL_VERSION` constant in `scripts/concat.py`, kept in
lockstep with `metadata.version` in this SKILL.md by the CI version-bump
workflow. Each input's `hash` is a sha256 over its stored bytes, so
renamed-but-unchanged inputs still match and same-named-but-modified inputs
do not.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/concat.py -i /tmp/cf.zarr -i /tmp/pf.zarr --dim number --output /tmp/ens.zarr
```
