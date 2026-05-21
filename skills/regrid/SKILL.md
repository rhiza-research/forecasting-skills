---
name: regrid
description: Linearly regrid a Rhiza Envelope Zarr onto a target grid defined by a resolution and an offset (target points at offset + k*resolution). Use when a task needs to bring a gridded dataset onto a specific grid alignment to compare or combine with another dataset.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# regrid

Source-agnostic spatial regridding: linearly interpolates the input onto a
uniform target grid whose points fall at `offset + k * resolution` for integer
k, clipped to the input's lon/lat range. Same library and call shape sheerwater
uses (`xarray-regrid`'s `.regrid.linear()` accessor).

`(--target-resolution 0.25, --offset 0.0)` aligns with sheerwater's
`global0_25`; `(0.1, 0.05)` with `global0_1`; `(0.05, 0.025)` with `global0_05`.

## When to use

- Bringing a dataset onto another dataset's grid alignment (CHIRPS 0.05° onto
  the IMERG 0.1° grid, ECMWF 1.5° onto a 0.25° analysis grid, etc.).
- Producing output on a named sheerwater grid by passing the matching
  `(resolution, offset)` pair.

Not for: statistical/bias-corrected downscaling — that's a domain-specific
operation tracked separately. Not for choosing a non-linear regridding method
(nearest, cubic, conservative, most_common); this skill is linear-only.

## Usage

```
uv run scripts/regrid.py --input <in.zarr> --output <out.zarr> \
    --target-resolution DEG --offset DEG \
    [--variable NAME] [--dims LAT,LON]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded envelope).
- `--output`, `-o` — output Zarr.
- `--target-resolution` — target grid spacing in degrees.
- `--offset` — grid offset in degrees; target points fall at `offset + k*resolution`.
- `--variable`, `-v` — restrict to a single data variable. Default: regrid all.
- `--dims` — comma-separated lat,lon dim names. Defaults autodetect via CF metadata.

### Longitude convention

Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before the target axis is built, so a global grid stored in the `[0, 360]` convention does not produce a target axis spanning the entire globe when only a sub-region is wanted. Inputs already in `[-180, 180]` pass through unchanged.

### Output

Same shape as input except the lat/lon dims are replaced by the target grid.
Non-spatial dims (`number`, `step`, `time`) are preserved. CF metadata on
lat/lon and data variables is preserved.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `rhiza_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes, so a renamed-but-unchanged input still cache-hits and a
same-named-but-modified input correctly cache-misses; `version` is the
git sha of the script at run time, or `"unknown"` when not resolvable. Cache-hit comparison reads the existing output's
`rhiza_history`: a hit requires the upstream entries to match and the last
entry's `skill`, `args`, and `input` to match the proposed new entry. The
previously stamped scalars `rhiza_regrid_resolution`, `rhiza_regrid_offset`,
`rhiza_regrid_method`, and `rhiza_inputs` are no longer written — they were
collision-prone across a chain and are recoverable from `rhiza_history`.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `anchor_end`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--anchor-end`). A consumer
reconstructing a `uv run scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Examples

```bash
# Onto sheerwater's global0_25 alignment.
uv run scripts/regrid.py -i /tmp/imerg.zarr -o /tmp/imerg_p25.zarr \
    --target-resolution 0.25 --offset 0.0
```

```bash
# Onto sheerwater's global0_1 alignment.
uv run scripts/regrid.py -i /tmp/chirps.zarr -o /tmp/chirps_p1.zarr \
    --target-resolution 0.1 --offset 0.05
```
