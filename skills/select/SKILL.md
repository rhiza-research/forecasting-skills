---
name: select
description: Select entries along one named dimension of a weather-skills standard dataset Zarr, by integer position or by coordinate value. A single selection collapses the dimension and drops the coordinates it leaves scalar, so outputs from different sources are ready to concat — e.g. pick the same forecast week from several model datasets before merging them along a new model dim.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py *)
metadata:
  catalog-group: transforms
---

# select

Entry-selection primitive. Picks entries along one named dimension of an
standard dataset, either by integer position (`--index`) or by coordinate value
(`--value`), and writes a new standard dataset. A single selection collapses the
dimension and also drops every coordinate variable it leaves scalar; multiple
selections keep the dimension with just those entries, in the order given.
Everything else — untouched dims, coords, data variables, and attrs — passes
through unchanged. Works on gridded and point_obs datasets alike.

## When to use

- To align inputs for `concat`: select the same entry (e.g. week-1, `--dim
  step --index 0`) from each of several forecast datasets, then concatenate
  the selected outputs along a new `model` dim. The collapse-and-drop
  semantics make the outputs merge cleanly — a leftover scalar coord on the
  selected dim would otherwise block or pollute the merge.
- To pull a single ensemble member, forecast step, timestamp, or station out
  of a larger standard dataset.
- To subset a dimension to a handful of entries in a chosen order.
- Before `convert-to-totals`, when the time/step axis still overlaps
  (rolling `--window`, or more intervals than you want as independent
  totals). Pick a non-overlapping subset (`--dim time` or `--dim step`,
  `--index` or `--value`); convert-to-totals refuses overlapping labels
  rather than silently thinning the axis.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py --input <in.zarr> --output <out.zarr> \
    --dim DIM (--index N [--index N ...] | --value V [--value V ...])
```

### Arguments

- `--input`, `-i` — input Zarr.
- `--output`, `-o` — output Zarr. Must be a distinct path: the same store as
  the input (or one nested inside the other) is rejected. An existing path
  here must be a directory (a store to replace); a plain file is rejected.
- `--dim` — the dimension to select along (exactly one per run).
- `--index` — integer position to select. Repeat once per position. Strict
  integers only (ASCII digits; no floats, signs other than a leading minus,
  or separators); negative positions count from the end (`-1` is the last
  entry). Mutually exclusive with `--value`.
- `--value` — coordinate value to select. Repeat once per value. Mutually
  exclusive with `--index`.

### Selector semantics

`--index` positions addressing the same element are an error — including a
negative alias of an already-given position (on a size-4 dim, `--index 0
--index -4` name the same element). An out-of-range position errors naming the
position, the dim, and its size.

`--value` parses each literal against the dim coord's dtype:

| Coord dtype | Literal form | Example |
| --- | --- | --- |
| `datetime64` | naive ISO datetime string (no timezone suffix, no `now`/`today`) | `2026-06-01`, `2026-06-01T06:00` |
| `timedelta64` | pandas-style timedelta string | `7D`, `168h` |
| numeric | int or float literal (no underscores, `nan`, or `inf`) | `0`, `1.5` |
| string | verbatim | `TA00001` |

Matching is exact (no nearest-neighbor lookup). A value absent from the coord
errors with a sample of the available values; a value matching more than one
entry (duplicate coord labels) errors as ambiguous; a literal unparseable for
the coord's dtype errors naming the dtype and the expected form; two values
addressing the same element error. A dim that has no coordinate variable
rejects `--value` with an error directing to `--index`.

### Output

A single selection (one `--index` or one `--value`) collapses the dim — it
disappears from the output — AND drops every coordinate variable the
selection leaves scalar: the dim's own coord and any auxiliary coord on the
collapsed dim (e.g. a `valid_time(step)` coord goes with `step`). Coordinates
that were already scalar on the input pass through. The output is therefore
ready to `concat` along a new dim. Multiple selections keep the dim with
exactly those entries, in the order the flags were given (args order = output
order; `--index 2 --index 0` reverses those two entries). Selecting by
`--value` produces the same data as selecting the corresponding positions by
`--index`.

All other dims, coords, data variables, values, and `weather_skills_*` attrs pass
through unchanged. On any argument, input, or selection validation error —
missing or unreadable input, an output path that exists as a plain file,
unknown dim, bad selector — the skill exits with code 2 and a clear `Error:`
message; no output is produced.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: the input's chain plus
an entry for this run, each entry `{skill, version, args, input}` (the
`version` recorded in this skill's own entry is the value printed by its
`--help`; inherited upstream entries carry their own versions). Repeated
`--index`/`--value` flags are recorded as a list in the order given, since
that order determines the output; index positions are recorded as canonical
integers (`0` and `00` are the same selection). Inspect a written output's
lineage with the `provenance` skill.

Re-running with identical arguments against an unchanged input and an existing
output is a cheap no-op — reuse the same output path. A cache hit requires the
same skill `version`, the same flags (in the same order), the same input name,
the same input content, and the same upstream history; any modification to the
input forces a recompute (a renamed-but-unchanged input misses, and a modified
same-named input misses).

## Example

Pick week-1 from two weekly forecast datasets, then merge the selected
outputs along a new `model` dim with the `concat` skill
(`--dim model --coords ECMWF,GFS`):

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py -i /tmp/ecmwf_weekly.zarr -o /tmp/ecmwf_w1.zarr \
    --dim step --index 0
uv run ${CLAUDE_SKILL_DIR}/scripts/select_dim.py -i /tmp/gfs_weekly.zarr -o /tmp/gfs_w1.zarr \
    --dim step --index 0
```

Each output has no `step` dim and no `step` coord, so the two stores differ
only along the new `model` axis when concatenated.
