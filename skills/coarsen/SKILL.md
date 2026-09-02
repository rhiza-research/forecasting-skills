---
name: coarsen
description: Coarsen or align a weather-skills standard dataset Zarr by linearly interpolating it onto a target grid. Prefer --reference-grid PATH to copy another Zarr's exact lat/lon (avoids float mismatch on difference/verify). Or pass --target-resolution and --offset for a synthetic grid (points at offset + k*resolution). Geometry-only — changes spacing/alignment, adds no information. Use to make a grid coarser or to put two datasets on the same grid for comparison.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# coarsen

Source-agnostic spatial coarsening and alignment: linearly interpolates the
input onto a target lat/lon grid. This changes grid geometry only — it adds
no information — and is used to coarsen a grid or to align two grids for
comparison. The target must be coarser-or-equal to the input on each axis;
equal resolution is accepted as a no-op/realign. A strictly-finer target is
rejected with a pointer to the `downscale` skill.

**Matching another dataset.** Prefer `--reference-grid other.zarr`. That
copies the other store's lat/lon values bit-for-bit (clipped to the input
extent), so `difference` / `verify` inner-joins do not collapse from
floating-point mismatch. Rebuilding `offset + k * resolution` in float64
often lands *near* IMERG/CHIRPS/ECMWF points without matching them exactly.

**Synthetic sheerwater-style grids.** When you have no reference Zarr,
`(--target-resolution 0.25, --offset 0.0)` aligns with `global0_25`;
`(0.1, 0.05)` with `global0_1`; `(0.05, 0.025)` with `global0_05`.

## When to use

- Putting obs onto a forecast grid (or the reverse) before `difference` /
  `verify` — use `--reference-grid` on the dataset you want to match.
- Coarsening to a larger spacing before plotting or ensemble aggregation.
- Producing output on a named sheerwater grid via `(resolution, offset)`.

Not for: making a grid finer / adding information — that is the `downscale`
skill. Not for choosing a non-linear method (nearest, cubic, conservative,
most_common); this skill is linear-only.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py --input <in.zarr> --output <out.zarr> \
    (--reference-grid REF.zarr | --target-resolution DEG --offset DEG) \
    [--variable NAME]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded dataset).
- `--output`, `-o` — output Zarr.
- `--reference-grid` — Zarr whose lat/lon define the target (exact values,
  clipped to the input extent). Preferred when matching another product.
  Mutually exclusive with `--target-resolution` / `--offset`. The reference
  must be coarser-or-equal to the input; if it is finer, use
  `downscale --reference-grid` instead.
- `--target-resolution` — target grid spacing in degrees (with `--offset`).
- `--offset` — grid offset in degrees; target points fall at
  `offset + k*resolution` (with `--target-resolution`).
- `--variable`, `-v` — restrict to a single data variable. Default: process all.

### Longitude convention

Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before the target
axis is built (input and `--reference-grid`), so a global grid stored in the
`[0, 360]` convention does not produce a target axis spanning the entire
globe when only a sub-region is wanted. Inputs already in `[-180, 180]` pass
through unchanged.

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
`scripts/coarsen.py`. Cache-hit comparison reads the existing
output's `weather_skills_history`: a hit requires the upstream chain to match and the
last entry's `skill`, `version`, `args`, and `input.basename` to match the
proposed new entry; on a hit the script returns without recomputing. The
`input.hash` is not part of the cache key — the comparison rests on basename,
args, and the upstream chain, and the input content is not re-hashed for the
cache decision (so a same-named input whose content changed in place still
hits on basename).

The `args` dict stores argparse dest names (underscored, e.g.
`target_resolution`, `offset`, `reference_grid`), not the hyphenated CLI flag
names. A consumer reconstructing a
`uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must translate
underscore → hyphen.

## Examples

```bash
# Match IMERG's exact 0.1° coordinates (avoids float mismatch on difference).
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/forecast.zarr -o /tmp/forecast_on_imerg.zarr \
    --reference-grid /tmp/imerg.zarr
```

```bash
# Obs onto the forecast grid before verify (forecast is coarser-or-equal).
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/chirps.zarr -o /tmp/chirps_on_s2s.zarr \
    --reference-grid /tmp/s2s.zarr
```

```bash
# Onto sheerwater's global0_25 alignment (no reference Zarr).
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/imerg.zarr -o /tmp/imerg_p25.zarr \
    --target-resolution 0.25 --offset 0.0
```

```bash
# Onto sheerwater's global0_1 alignment (no reference Zarr).
uv run ${CLAUDE_SKILL_DIR}/scripts/coarsen.py -i /tmp/chirps.zarr -o /tmp/chirps_p1.zarr \
    --target-resolution 0.1 --offset 0.05
```
