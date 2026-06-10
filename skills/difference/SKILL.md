---
name: difference
description: Subtract one Rhiza Envelope Zarr from another (A − B) with xarray inner-join alignment and broadcasting — e.g. anomalies as a field minus its baseline mean, or a scenario-minus-historical change map. Use whenever two envelopes must be compared cell-by-cell as a difference field.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.0"
---

# difference

Source-agnostic subtraction of two envelopes: the first input is the minuend
(A), the second the subtrahend (B). Subtraction is xarray-aligned — inner
join on shared dims, broadcasting over dims present on only one side — so a
`(time, latitude, longitude)` field minus a `(latitude, longitude)` baseline
(e.g. a time-mean from `reduce`) yields per-time anomalies.

## When to use

- Anomaly vs climatology: a field minus its baseline mean (e.g. SST
  anomalies as `sst.zarr` minus a `reduce --dim time --method mean`
  baseline).
- Scenario minus historical: a change map (e.g. a CMIP6 SSP time-mean minus
  the historical time-mean = projected change by 2050).
- Any cell-by-cell difference of two datasets on a shared grid (forecast
  minus observations, model A minus model B).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i a.zarr -i b.zarr --output <out.zarr> \
    [--variable VAR ...]
```

The output must be a distinct store from both inputs; the skill rejects a run
where `--output` resolves to either `--input` path.

### Arguments
- `--input`, `-i` — input Zarr; pass exactly twice. The first is A (the
  minuend), the second is B (the subtrahend); any other count exits non-zero.
- `--output`, `-o` — output Zarr.
- `--variable`, `-v` — repeatable; restricts the difference to the named data
  variable(s). Each name must be a data variable of BOTH inputs; a violation
  exits non-zero and lists each input's data variables. Default (unset)
  differences every data variable present in both inputs (in the first
  input's order); inputs sharing no data variable exit non-zero. Data
  variables not differenced (absent from an input, or unselected) are dropped
  from the output with a stderr note.

### Alignment

Shared dims are aligned with an inner join, so only overlapping coordinate
values participate; dims present on only one input broadcast. When a variable
ends up empty along a dim the skill exits non-zero (no output is written) and
distinguishes the two causes: a dim that was already empty in an input before
alignment, versus a dim left empty because alignment found no overlapping
coordinate values.

A shared dim that has no index coordinate cannot be label-aligned, so it is
paired positionally (element *i* of A minus element *i* of B). If the two
inputs disagree on such a dim's size, the run exits non-zero naming the dim
(unlabeled rows of different length cannot be aligned); if the sizes are
equal, the run proceeds but warns on stderr that the pairing is positional, so
you can confirm the rows actually correspond.

### Operand dtypes

Boolean and unsigned-integer variables are cast before subtracting: boolean
becomes a signed integer (bool subtraction is nonsensical), and an unsigned
integer is promoted to a signed integer wide enough to hold negatives, so a
result that should be negative is not wrapped around modulo `2**nbits`. Signed
integer, floating-point, and other dtypes are left untouched.

### Input units

The output variable keeps the first input's attrs, including its `units`.
When the two inputs carry a variable's `units` attr in differing values, the
subtraction would mix incompatible scales, so a warning naming each input (by
its full path) and its units is printed to stderr and the run proceeds —
convert the inputs onto one units basis with `unit-convert` first. The check
keys on the full input path rather than the basename, so two inputs that share
a filename in different directories are still compared as distinct inputs.
Only string `units` values are compared, after stripping surrounding
whitespace; an input that omits `units` is not treated as a difference.

### Output

One data variable per differenced variable, holding A − B on the aligned
(and broadcast) dims. Dataset attrs from the first input are preserved.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. Because difference takes
two inputs, its entry's `input` field is a list of `{basename, hash, history}`
dicts (one per input, in the order given on the command line: A then B). Each
item's `history` holds that input's full `rhiza_history` chain (an empty list
when the input had no `rhiza_history`), so the difference entry records every
input branch and the output is fully reproducible from its own provenance.
The output's top-level `rhiza_history` is a single linear array: the first
input's chain followed by this difference entry, matching the attr
passthrough already done on the dataset. `args` is the argparse namespace
minus the `--input`/`--output` path strings (with `--variable` deduped and
sorted, so reordered or repeated flags stamp identically); `version` is the
skill's version, as printed by `--help`. Each input's `hash` is a sha256 over
its stored bytes.

A cache hit requires the same skill `version`, the same flags, the same input
names (each `basename`), the same input content (each `hash`), and the same
upstream history; any modification to either input forces a recompute.
Concretely, a renamed-but-unchanged input misses (the basename differs) and a
modified same-named input misses (the content hash differs). Cache-hit
comparison reads the existing output's `rhiza_history`: a hit requires the
upstream entries to match and the last entry's `skill`, `version`, `args`, and
per-input `{basename, hash, history}` to match the proposed new entry.

## Examples

SST anomalies are a two-skill recipe: build the time-mean baseline with the
`reduce` skill, then subtract it with this skill. Each script runs under its
own skill's directory — `reduce.py` from the `reduce` skill (its
`${CLAUDE_SKILL_DIR}`), `difference.py` from this one — so the reduce step is
*not* invoked under difference's `${CLAUDE_SKILL_DIR}`.

```bash
# Step 1 — reduce skill: time-mean baseline.
uv run ${REDUCE_SKILL_DIR}/scripts/reduce.py -i /tmp/sst.zarr -o /tmp/sst_baseline.zarr \
    --dim time --method mean

# Step 2 — difference skill: field minus its baseline (broadcast over time).
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i /tmp/sst.zarr -i /tmp/sst_baseline.zarr \
    --output /tmp/sst_anom.zarr
```

```bash
# Projected change: scenario time-mean minus historical time-mean.
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i /tmp/ssp245_mean.zarr -i /tmp/historical_mean.zarr \
    --output /tmp/change_2050.zarr
```
