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
values participate; dims present on only one input broadcast. When alignment
leaves a variable empty along a dim (no overlapping coordinate values), the
skill exits non-zero naming the variable and the empty dim(s); no output is
written.

### Input units

The output variable keeps the first input's attrs, including its `units`.
When the two inputs carry a variable's `units` attr in differing values, the
subtraction would mix incompatible scales, so a warning naming each input and
its units is printed to stderr and the run proceeds — convert the inputs onto
one units basis with `unit-convert` first. Only string `units` values are
compared, after stripping surrounding whitespace; an input that omits `units`
is not treated as a difference.

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
minus the `--input`/`--output` path strings; `version` is the
`_RHIZA_SKILL_VERSION` constant in `scripts/difference.py`, kept in lockstep
with `metadata.version` in this SKILL.md by the CI version-bump workflow.
Each input's `hash` is a sha256 over its stored bytes, so
renamed-but-unchanged inputs still match and same-named-but-modified inputs
do not. Cache-hit comparison reads the existing output's `rhiza_history`: a
hit requires the upstream entries to match and the last entry's `skill`,
`args`, and per-input `{basename, history}` to match the proposed new entry.

## Examples

```bash
# SST anomalies: field minus its time-mean baseline (broadcast over time).
uv run ${CLAUDE_SKILL_DIR}/scripts/reduce.py -i /tmp/sst.zarr -o /tmp/sst_baseline.zarr \
    --dim time --method mean
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i /tmp/sst.zarr -i /tmp/sst_baseline.zarr \
    --output /tmp/sst_anom.zarr
```

```bash
# Projected change: scenario time-mean minus historical time-mean.
uv run ${CLAUDE_SKILL_DIR}/scripts/difference.py -i /tmp/ssp245_mean.zarr -i /tmp/historical_mean.zarr \
    --output /tmp/change_2050.zarr
```
