---
name: downscale
description: Downscale a weather-skills envelope Zarr onto a finer-or-equal grid, adding information via a chosen --method (linear-interpolation or q-q empirical quantile mapping). The target is given by an integer factor, a target resolution, or a reference dataset's grid. Equal resolution is accepted as a no-op on geometry (q-q still applies its mapping). Use when a task needs higher spatial resolution; to make a grid coarser, use the coarsen skill.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.8"
  catalog-group: transforms
---

# downscale

Source-agnostic spatial downscaling: produces a grid finer than or equal to
the input's and adds information through a pluggable `--method`. The target grid is
specified one of three ways — an integer `--factor` (new spacing = input
spacing / factor), a `--target-resolution` in degrees, or a `--reference-grid`
dataset whose lat/lon grid becomes the target. The requested target must be
finer-or-equal to the input on each axis; equal resolution is accepted as a
no-op on geometry (for `--method q-q` the value mapping still applies). A
strictly-coarser target is rejected with a pointer to the `coarsen` skill.

Methods:

- `linear-interpolation` — linearly interpolate the input onto the finer target
  grid (via `xarray-regrid`'s `.regrid.linear()` accessor). No distribution
  change.
- `q-q` — linearly interpolate onto the finer target grid, then apply per-grid-cell
  empirical quantile-quantile mapping along `--time-dim`, mapping the
  interpolated values onto the distribution of a `--qq-reference` dataset that
  must already be on the output (finer) grid.

## When to use

- A gridded Zarr needs higher spatial resolution before plotting or comparison.
- Matching the (finer) resolution of another dataset via its grid
  (`--reference-grid`).
- Bias-correcting interpolated output against an observational reference on the
  output grid (`--method q-q` with `--qq-reference`).

Not for: coarsening a grid onto a strictly-coarser resolution — that is the
`coarsen` skill.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/downscale.py --input <in.zarr> --output <out.zarr> \
    --method {linear-interpolation,q-q} \
    (--factor N | --target-resolution DEG | --reference-grid REF.zarr) \
    [--dims LAT,LON] [--variable NAME] \
    [--qq-reference REF.zarr] [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded envelope).
- `--output`, `-o` — output Zarr.
- `--method` — `linear-interpolation` or `q-q`. Required.
- `--factor`, `-f` — integer refinement factor (>= 1). New spacing = input spacing / factor (factor 1 = identity). Mutually exclusive with `--target-resolution` and `--reference-grid`.
- `--target-resolution` — target spacing in degrees; must be finer-or-equal (<=) to the input on each axis. Mutually exclusive with `--factor` and `--reference-grid`.
- `--reference-grid` — path to a reference Zarr whose lat/lon grid defines the target. The reference grid must be finer-or-equal to the input. Mutually exclusive with `--factor` and `--target-resolution`.
- `--dims` — comma-separated lat,lon dim names. Defaults autodetect among `latitude/lat/y` and `longitude/lon/x`.
- `--variable`, `-v` — restrict to a single data variable. Default: process all.
- `--qq-reference` — reference Zarr whose distribution the `q-q` method maps the output onto. Per-grid-cell empirical quantile mapping along `--time-dim`. The reference must already be on the post-downscale lat/lon grid; mismatches are an error. Required for `--method q-q`.
- `--time-dim` — time dimension used as the sample axis for q-q mapping. Default: `time`. Both the output and the reference must have a dimension by this name.

### Output

Same shape as input except the lat/lon dims are finer. Non-spatial dims
(`number`, `step`, `time`) are preserved. With `--method q-q`, only data
variables present in both the interpolated output and the `--qq-reference` are
mapped; others pass through unchanged.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. This skill reads the upstream
input's `weather_skills_history` (default `[]` with a stderr warning if absent) and
appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings — so `method`, `factor`, `target_resolution`,
`reference_grid`, `dims`, `variable`, `qq_reference`, and `time_dim` are
recorded under their argparse dest names (underscored). `input` is a
`{basename, hash}` dict for `--input`. When `--reference-grid` and/or
`--qq-reference` are supplied, the entry also carries a `reference_inputs`
field: a list of `{basename, hash}` dicts content-hashing each supplied
reference zarr's stored bytes, so editing a reference in place (same path,
changed content) invalidates the cache and forces a recompute. `version` is
the `_SKILL_VERSION` constant in `scripts/downscale.py`, kept in lockstep
with `metadata.version` in this SKILL.md by the CI version-bump workflow.
Cache-hit comparison reads the existing output's `weather_skills_history`: a hit requires
the upstream chain to match and the last entry's `skill`, `version`, `args`,
`input.basename`, and `reference_inputs` to match the proposed new entry; on a
hit the script returns without recomputing. The main `--input` hash is not part
of the cache key (a renamed-but-unchanged input still hits on basename), but the
secondary `reference_inputs` hashes are.

## Examples

```bash
# Factor-4 finer, linear interpolation.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_4x.zarr \
    --method linear-interpolation --factor 4
```

```bash
# Onto a finer 0.05° grid, linear interpolation.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/imerg.zarr -o /tmp/imerg_p05.zarr \
    --method linear-interpolation --target-resolution 0.05
```

```bash
# Match the (finer) grid of another dataset.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_on_imerg.zarr \
    --method linear-interpolation --reference-grid /tmp/imerg.zarr
```

```bash
# Downscale ECMWF onto a finer 0.05° grid and q-q map onto ERA5 observations
# that already sit on that grid.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_p05_qq.zarr \
    --method q-q --target-resolution 0.05 --qq-reference /tmp/era5_p05.zarr
```
