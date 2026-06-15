---
name: difference
description: Subtract one weather-skills envelope Zarr from another (A − B) with xarray inner-join alignment and broadcasting — e.g. anomalies as a field minus its baseline mean, or a scenario-minus-historical change map. Use whenever two envelopes must be compared cell-by-cell as a difference field.
license: MIT
compatibility: Requires Python 3.12 and uv.
metadata:
  version: "0.1.4"
  catalog-group: transforms
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
uv run --script ${CLAUDE_SKILL_DIR}/scripts/difference.py -i a.zarr -i b.zarr --output <out.zarr> \
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

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array
of per-step entries `{skill, version, args, input}` (the `version` recorded in
this skill's own entry is the value printed by its `--help`; inherited upstream
entries carry their own versions). Because difference takes two inputs, its entry's `input`
is a list with one item per input (A then B), each carrying that input's full
upstream chain, so both branches are recorded; the top-level chain is the first
input's chain followed by the difference entry. Inspect a written output's
lineage with the `provenance` skill.

Re-running with identical arguments against unchanged inputs and an existing
output is a cheap no-op — reuse the same output path. A cache hit requires the
same skill `version`, the same flags, the same input names, the same input
content, and the same upstream history; any modification to either input forces
a recompute (a renamed-but-unchanged input misses, and a modified same-named
input misses).

## Examples

SST anomalies are a two-skill recipe: first the `reduce` skill produces a
time-mean baseline (e.g. `/tmp/sst_baseline.zarr` from `/tmp/sst.zarr`), then
this skill subtracts that baseline from the field, broadcasting over time. Only
the `difference` step is shown here; run the `reduce` skill separately to make
the baseline.

```bash
# Field minus its time-mean baseline (the baseline produced by the reduce
# skill), broadcast over time.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/difference.py -i /tmp/sst.zarr -i /tmp/sst_baseline.zarr \
    --output /tmp/sst_anom.zarr
```

```bash
# Projected change: scenario time-mean minus historical time-mean.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/difference.py -i /tmp/ssp245_mean.zarr -i /tmp/historical_mean.zarr \
    --output /tmp/change_2050.zarr
```
