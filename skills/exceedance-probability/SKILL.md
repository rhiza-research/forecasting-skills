---
name: exceedance-probability
description: Compute the percentage of ensemble members (along a named dim) whose forecast value satisfies a comparison against a fixed threshold, e.g. "chance that week-1 total precip exceeds 28mm." Use whenever a dataset needs ensemble exceedance probability for a scalar threshold.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/exceedance_probability.py *)
metadata:
  version: "0.1.0"
  catalog-group: transforms
---

# exceedance-probability

Source-agnostic ensemble exceedance probability along a named dim. For each
selected data variable, computes the percentage of entries along `--dim`
satisfying `value <comparison> threshold`, per remaining grid cell/step. Data
variables that don't carry `--dim` pass through untouched.

## When to use

- Chance of a threshold event: "probability that week-1 total precip exceeds
  28mm" — `--dim number --threshold 28 --comparison ge` on an ECMWF/GEFS
  ensemble forecast.
- Any other named-dim exceedance: chance of a heatwave day (`--comparison ge`
  on temperature), chance of staying below a minimum (`--comparison lt`).

This skill takes a **fixed scalar** threshold only — it does not read a
per-gridcell threshold from a second Zarr. For a climatology-relative
threshold, compute the difference against the climatology first (`difference`
skill) and apply `exceedance-probability --threshold 0` to the result.

This skill has no time-window concept. "Chance of exceeding 28mm in week 1"
vs. "in month 1" is encoded in the *input Zarr's* time-bin shape — resample
first with `aggregate-temporal --period weekly|monthly --method sum`, then
feed the result to this skill.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/exceedance_probability.py \
    --input <in.zarr> --output <out.zarr> \
    --dim DIM --threshold FLOAT --comparison gt|ge|lt|le \
    [--variable VAR ...]
```

The output must be a distinct store from the input; the skill rejects a run
where `--input` and `--output` resolve to the same path.

### Arguments
- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr (a distinct path from `--input`).
- `--dim` — the ensemble/member dimension to compute the percentage over
  (e.g. `number` for ECMWF/GEFS forecast envelopes). Must be a dim of the
  input.
- `--threshold` — value to compare each member against, in the target
  variable's own units. No unit conversion happens in this skill; use
  `unit-convert` upstream if needed.
- `--comparison` — the comparison applied as `value <op> threshold`: `gt`
  (greater than), `ge` (greater than or equal), `lt` (less than), or `le`
  (less than or equal).
- `--variable`, `-v` — repeatable; restricts the computation to the named
  data variable(s). Each name must be a data variable of the input and must
  carry `--dim`; violations exit non-zero. Default (unset) computes over
  every data variable carrying `--dim`. Unselected or untouched data
  variables pass through unchanged (a stderr note lists them); computing
  against a default selection where no data variable carries `--dim` exits
  non-zero.

### Output

Each selected variable becomes the percentage (0-100) of `--dim` entries
satisfying the comparison, with `--dim` collapsed. Output attrs are rebuilt
from scratch rather than carried over from the source variable: `units` is
set to `%`, and `long_name`/`GRIB_name` are both set to a descriptive label
built from the comparison (e.g. `"probability tp ge 28mm"`) — `long_name` is
set explicitly because `plot`'s colorbar-label resolution checks it before
`GRIB_name`. No `standard_name` is set: the source variable's `standard_name`
(e.g. `precipitation_amount`) describes the input physical quantity, not the
derived percentage, and CF has no `standard_name` for "probability of
exceeding a threshold" to verify against. The collapsed dim disappears from
the output (along with its coordinates) once no data variable carries it; a
dim still carried by a pass-through variable stays. Remaining dims, coords,
and pass-through variables are unchanged.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: the input's chain plus
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
# Chance that week-1 total precip exceeds 28mm across the ECMWF ensemble.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/exceedance_probability.py \
    -i /tmp/ecmwf_week1.zarr -o /tmp/ecmwf_week1_p28.zarr \
    --dim number --threshold 28 --comparison ge
```

```bash
# Chance of staying below a 2mm dry-day threshold, restricted to `tp`.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/exceedance_probability.py \
    -i /tmp/ecmwf.zarr -o /tmp/ecmwf_dry.zarr \
    --dim number --threshold 2 --comparison lt --variable tp
```
