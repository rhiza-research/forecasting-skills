---
name: unit-convert
description: Convert one data variable in a weather-skills envelope Zarr to a target units string (e.g. a precipitation flux `kg m-2 s-1` to a depth rate `mm/day`), updating the variable's values and its `units` attribute.
license: MIT
compatibility: Requires Python 3.12+ and uv.
metadata:
  version: "0.1.5"
  catalog-group: transforms
---

# unit-convert

Source-agnostic units conversion primitive. Reads a variable's `units` attr,
converts the values to `--to-units` with the `pint` units library, and writes a
new Zarr whose variable carries the converted values and the target `units`
string.

## When to use

- A variable's values sit on the wrong scale for a downstream consumer — for
  example a precipitation flux in `kg m-2 s-1` (values ~0–0.003) that lands
  entirely in the lowest bin of a categorical `mm` colormap, or a depth in `m`
  that needs to be `mm` before comparison.
- Any time two datasets must share a units basis before they can be compared,
  concatenated, or plotted on one scale.

## Conversion model

Conversion runs through the actual array as a pint `Quantity`, so offset units
are handled correctly (`K` → `degC` subtracts 273.15, it is not a pure scale).

Cross-dimension water conversions go through a liquid-water density bridge of
1000 kg m**-3 — that is, 1 kg m**-2 of water equals 1 mm of depth. A direct
conversion is attempted first; if the source and target dimensions don't match,
the source quantity is retried divided and then multiplied by water density. So
a flux `kg m-2 s-1` converts to a depth rate `mm/day` (× 86400 via the bridge),
and an accumulated `kg m-2` converts to `mm` (1:1). Source and target unit
strings are accepted in CF/UDUNITS power notation (`kg m-2 s-1`, `W m-2`); the
skill rewrites that to the form pint parses.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py --input <in.zarr> --output <out.zarr> \
    --to-units <UNITS> [--variable NAME] [--standard-name NAME]
```

The output must be a distinct store from the input; the skill rejects a run
where `--input` and `--output` resolve to the same path.

### Arguments
- `--input`, `-i` — input Zarr containing a weather-skills envelope.
- `--output`, `-o` — output Zarr (a distinct path from `--input`).
- `--to-units` — target units string. Becomes the output variable's `units`
  attr verbatim.
- `--variable`, `-v` — variable to convert. If omitted and the input has a
  single data variable, that one is used. If multiple data vars are present,
  `--variable` is required.
- `--standard-name` — CF `standard_name` to write on the output variable. See
  the `standard_name` handling below.

### Output

Same dims, coords, and data variables as the input. The selected variable's
values are converted to the target units and its `units` attr is set to the
`--to-units` string; its other attrs are preserved. All other variables are
unchanged.

#### `standard_name` handling

CF `standard_name` names the physical quantity, so a conversion that changes
dimensionality (e.g. a flux `kg m-2 s-1` → a depth rate `mm/day`) can leave the
source name wrong. The output `standard_name` is resolved in this order:

1. `--standard-name` if given — written verbatim (an empty value drops the
   name).
2. else a built-in lookup keyed on the target units — `mm/day` →
   `lwe_precipitation_rate`, `kg m-2 s-1` → `precipitation_flux`. The lookup is
   spelling-robust (`mm/day`, `mm/d`, `mm day-1` match the same entry). Both
   entries name a precipitation quantity, so the lookup is applied only when the
   source variable has no `standard_name` or already names a precipitation
   quantity — a non-precipitation flux (e.g. an evaporation flux) converted to
   these units is not relabeled as precipitation; it falls through to step 3.
3. else, when the conversion changes dimensionality, the source
   `standard_name` is dropped (it no longer matches the units).
4. else (a same-dimension conversion, e.g. `m` → `mm`) the source
   `standard_name` is preserved.

Pass `--standard-name` for any conversion the lookup doesn't cover.

The skill exits with code 2 and a clear message when: the selected variable has
no `units` attr (nothing to convert from); the source and target units are
dimensionally incompatible and no water bridge reconciles them; either units
string is unparseable; or the input has multiple data vars and no `--variable`
is given.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. This skill reads the upstream
input's `weather_skills_history` (default `[]` and stderr warning if absent) and appends
its own entry. `args` is the argparse namespace minus the `--input`/`--output`
path strings; `input` is a `{basename, hash}` dict — `basename` is the upstream
zarr's filename and `hash` is a sha256 of its stored bytes, so a
renamed-but-unchanged input still cache-hits and a same-named-but-modified input
correctly cache-misses; `version` is the `_SKILL_VERSION` constant in
`scripts/unit-convert.py`, kept in lockstep with `metadata.version` in this
SKILL.md by the CI version-bump workflow.

The `args` dict stores argparse dest names (underscored, e.g. `to_units`), not
the hyphenated CLI flag names (`--to-units`). A consumer reconstructing a
`uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must translate
underscore → hyphen.

## Examples

```bash
# Precipitation flux -> depth rate so a forecast plots on the mm colormap.
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i /tmp/gfs.zarr -o /tmp/gfs_mm.zarr \
    --variable precipitation_surface --to-units 'mm/day'

# Depth in metres -> millimetres.
uv run ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i /tmp/tp_m.zarr -o /tmp/tp_mm.zarr \
    --to-units mm
```
