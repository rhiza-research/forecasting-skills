---
name: reduce
description: Collapse one or more named dimensions of a Rhiza Envelope Zarr with a statistic (mean, std, min, max, sum, median) — e.g. ensemble spread as the std across `number`, model disagreement as the std across a model dim, or a time-mean baseline for anomalies. Use whenever a dataset needs a statistical reduction along a named dimension.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.1"
  catalog-group: transforms
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

NaNs are skipped (xarray's default `skipna`). Two method-specific
conventions follow from that:

- `sum` uses `min_count=1`: a slice that is entirely missing yields `NaN`,
  not `0`, so "no data" is not silently reported as a real zero total.
- `std` uses `ddof=1` (the sample standard deviation), matching the
  ensemble-spread convention that spread across members is a sample estimate
  rather than a population sigma. A `std` over a size-1 dim is therefore zero
  by construction (a single sample has no spread); the skill warns on stderr
  in that case.

### Output

The selected data variables with the requested dims collapsed. A reduced dim
disappears from the output (along with its coordinates) once no data variable
carries it; a dim still carried by a pass-through variable stays. Remaining
dims, coords, and pass-through variables are unchanged.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: the input's chain plus
an entry for this run, each entry `{skill, version, args, input}` (`version`
is the value printed by `--help`). Flag values in `args` are recorded under
underscored names (e.g. a flag `--time-dim` is recorded as `time_dim`);
translate underscore → hyphen when reconstructing a CLI invocation. Inspect a
written output's lineage with the `provenance` skill.

Re-running with identical arguments against an unchanged input and an existing
output is a cheap no-op — reuse the same output path. A cache hit requires the
same skill `version`, the same flags, the same input name, the same input
content, and the same upstream history; any modification to the input forces a
recompute (a renamed-but-unchanged input misses, and a modified same-named
input misses).

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
