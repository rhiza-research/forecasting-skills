---
name: concat
description: Concatenate two or more Rhiza Envelope Zarr stores along a named dimension, optionally assigning coordinate values to the new axis. Use when combining ensemble members, stitching time windows, or merging per-country fetches into a single dataset.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# concat

Source-agnostic concatenation along a named dim. Inputs must share all other dims and variables. If the concat dim does not exist on the inputs, a new dim is created (use `--coords` to assign values).

## When to use

- Stitching per-country or per-date fetches into one Zarr.
- Joining two ensemble halves (cf and pf) along `number`.
- Glueing back two time windows after parallel processing.

## Usage

```
uv run scripts/concat.py --inputs a.zarr,b.zarr,... --dim DIM --output <out.zarr> \
    [--coords V1,V2,...]
```

### Arguments
- `--inputs` — comma-separated list of input Zarr paths (order is preserved).
- `--dim` — dimension name to concatenate along.
- `--coords` — optional comma-separated coord values to assign to the new dim. Length must match number of inputs; only used when `--dim` does not already exist on inputs.
- `--output`, `-o` — output Zarr.

### Output

A single Zarr with the concat dim extended. Attrs from the first input are preserved.

## Example

```bash
uv run scripts/concat.py --inputs /tmp/cf.zarr,/tmp/pf.zarr --dim number --output /tmp/ens.zarr
```
