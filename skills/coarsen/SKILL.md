---
name: coarsen
description: Coarsen or align a weather-skills envelope Zarr by linearly interpolating it onto a target grid defined by a resolution and an offset (target points at offset + k*resolution). Geometry-only — it changes grid spacing/alignment and adds no information. Use to make a grid coarser or to put two datasets on the same grid for comparison.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/coarsen.py *)
metadata:
  version: "0.1.11"
  catalog-group: transforms
---

# coarsen

Source-agnostic spatial coarsening and alignment: linearly interpolates the
input onto a uniform target grid whose points fall at `offset + k * resolution`
for integer k, clipped to the input's lon/lat range. This changes grid geometry
only — it adds no information — and is used to coarsen a grid or to align two
grids for comparison. The target resolution must be coarser-or-equal to the
input on each axis; equal resolution is accepted as a no-op/realign. A
strictly-finer target is rejected with a pointer to the `downscale` skill, since
gaining resolution with added information is that skill's job.

`(--target-resolution 0.25, --offset 0.0)` aligns with sheerwater's
`global0_25`; `(0.1, 0.05)` with `global0_1`; `(0.05, 0.025)` with `global0_05`.

## When to use

- Coarsening a dataset to a larger grid spacing before plotting, comparison, or
  ensemble aggregation.
- Aligning a dataset to another dataset's grid alignment for comparison (CHIRPS
  0.05° onto the IMERG 0.1° grid, ECMWF 1.5° onto a 0.25° analysis grid, etc.).
- Producing output on a named sheerwater grid by passing the matching
  `(resolution, offset)` pair.

Not for: making a grid finer / adding information — that is the `downscale`
skill. Not for choosing a non-linear method (nearest, cubic, conservative,
most_common); this skill is linear-only.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/coarsen.py --input <in.zarr> --output <out.zarr> \
    --target-resolution DEG --offset DEG \
    [--variable NAME] [--dims LAT,LON]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded envelope).
- `--output`, `-o` — output Zarr.
- `--target-resolution` — target grid spacing in degrees.
- `--offset` — grid offset in degrees; target points fall at `offset + k*resolution`.
- `--variable`, `-v` — restrict to a single data variable. Default: process all.
- `--dims` — comma-separated lat,lon dim names. Defaults autodetect via CF metadata.

### Longitude convention

Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before the target axis is built, so a global grid stored in the `[0, 360]` convention does not produce a target axis spanning the entire globe when only a sub-region is wanted. Inputs already in `[-180, 180]` pass through unchanged.

### Output

Same shape as input except the lat/lon dims are replaced by the target grid.
Non-spatial dims (`number`, `step`, `time`) are preserved. CF metadata on
lat/lon and data variables is preserved.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `weather_skills_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes; `version` is the `_SKILL_VERSION` constant in
`scripts/coarsen.py`, kept in lockstep with `metadata.version` in this SKILL.md
by the CI version-bump workflow. Cache-hit comparison reads the existing
output's `weather_skills_history`: a hit requires the upstream chain to match and the
last entry's `skill`, `version`, `args`, and `input.basename` to match the
proposed new entry; on a hit the script returns without recomputing. The
`input.hash` is not part of the cache key — the comparison rests on basename,
args, and the upstream chain, and the input content is not re-hashed for the
cache decision (so a same-named input whose content changed in place still
hits on basename).

The `args` dict stores argparse dest names (underscored, e.g.
`target_resolution`, `offset`), not the hyphenated CLI flag names
(`--target-resolution`, `--offset`). A consumer reconstructing a
`uv run --script ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must translate
underscore → hyphen.

## Examples

```bash
# Onto sheerwater's global0_25 alignment.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/imerg.zarr -o /tmp/imerg_p25.zarr \
    --target-resolution 0.25 --offset 0.0
```

```bash
# Onto sheerwater's global0_1 alignment.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/chirps.zarr -o /tmp/chirps_p1.zarr \
    --target-resolution 0.1 --offset 0.05
```
