---
name: reduce
description: Collapse one or more named dimensions of a Rhiza Envelope Zarr with a statistic (mean, std, min, max, sum, median) — e.g. ensemble spread as the std across `number`, model disagreement as the std across a model dim, or a time-mean baseline for anomalies. Use whenever a dataset needs a statistical reduction along a named dimension.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.0"
---

# reduce

Source-agnostic statistical reduction along named dims. Collapses the
requested dims of each selected data variable with one statistic; data
variables that carry none of the requested dims pass through untouched.
NaNs are skipped (xarray's default `skipna`).

## When to use

- Ensemble spread — "where is the forecast most/least certain":
  `--dim number --method std` on an ensemble forecast yields the spread
  field (low std = high certainty).
- Model disagreement: `--method std` across a model dim on a multi-model
  dataset (e.g. one built with `concat` along a new dim).
- Building a baseline for `difference`: `--dim time --method mean` yields a
  climatological-mean field to subtract from a time-resolved field for
  anomalies.
- Any other named-dim statistic: ensemble mean, spatial max, member median.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/reduce.py --input <in.zarr> --output <out.zarr> \
    --dim DIM [--dim DIM ...] --method mean|std|min|max|sum|median \
    [--variable VAR ...]
```

The output must be a distinct store from the input; the skill rejects a run
where `--input` and `--output` resolve to the same path.

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr (a distinct path from `--input`).
- `--dim` — dimension to collapse. Repeat once per dimension to collapse
  several in one run. Each name must be a dim of the input.
- `--method` — statistic applied along the collapsed dimension(s): `mean`,
  `std`, `min`, `max`, `sum`, or `median`.
- `--variable`, `-v` — repeatable; restricts the reduction to the named data
  variable(s). Each name must be a data variable of the input and must carry
  every requested `--dim`; violations exit non-zero. Default (unset) reduces
  every data variable that carries at least one of the requested dims, each
  over the subset of those dims it carries. Unselected or untouched data
  variables pass through unchanged (a stderr note lists them); reducing a
  default selection where no data variable carries any requested dim exits
  non-zero.

### Method and units

Every method keeps each variable's attrs (`keep_attrs`), so the output
variable carries the input's `units` unchanged. For `mean`, `std`, `min`,
`max`, and `median` that is physically correct — the statistic is in the same
units as the values. `sum` along a dim is the one case where the units
semantics can change (N summed values in `mm` are a total, not another `mm`
sample in the same sense); this skill performs no unit math or relabeling
either way — use `unit-convert` to restamp units when needed. (For temporal
totals specifically, `aggregate-temporal --method sum` handles the units
relabel.)

### Output

The selected data variables with the requested dims collapsed. A reduced dim
disappears from the output (along with its coordinates) once no data variable
carries it; a dim still carried by a pass-through variable stays. Remaining
dims, coords, and pass-through variables are unchanged.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `rhiza_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes, so a renamed-but-unchanged input still cache-hits and a
same-named-but-modified input correctly cache-misses; `version` is the
`_RHIZA_SKILL_VERSION` constant in `scripts/reduce.py`, kept in lockstep
with `metadata.version` in this SKILL.md by the CI version-bump workflow.
Cache-hit comparison reads the existing output's `rhiza_history`: a hit
requires the upstream entries to match and the last entry's `skill`, `args`,
and `input` to match the proposed new entry.

The `args` dict stores argparse dest names (underscored), not the hyphenated
CLI flag names. A consumer reconstructing a
`uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Examples

```bash
# Ensemble spread: where is the forecast most/least certain.
uv run ${CLAUDE_SKILL_DIR}/scripts/reduce.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_spread.zarr \
    --dim number --method std
```

```bash
# Time-mean baseline to feed `difference` for anomalies.
uv run ${CLAUDE_SKILL_DIR}/scripts/reduce.py -i /tmp/sst.zarr -o /tmp/sst_baseline.zarr \
    --dim time --method mean
```
