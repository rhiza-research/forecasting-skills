---
name: unit-convert
description: Convert data variables in a weather-skills standard dataset to target units (cf-units), or normalize recognized temp/precip variables to standard display units (degree_Celsius, mm day-1, mm) via --to-standard.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py *)
metadata:
  version: "0.1.8"
  catalog-group: transforms
---

# unit-convert

Converts values with **cf-units** (UDUNITS-2), including a liquid-water density
bridge so precip flux/amount (`kg m-2…`) can become `mm` / `mm day-1`.

## When to use

- Put two stores on one units basis before compare/concat/plot.
- Normalize temp/precip to the weather-skills display standard with `--to-standard`
  (temp → `degree_Celsius`, precip rate/flux → `mm day-1`, precip amount → `mm`).

Plots already call `to_standard_units` before rendering; use this skill when you
need a converted Zarr on disk.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i <in.zarr> -o <out.zarr> \
    --to-units <UNITS> [--variable NAME] [--standard-name NAME]

uv run --script ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i <in.zarr> -o <out.zarr> \
    --to-standard [--variable NAME]
```

### Arguments

- `--to-units` — target UDUNITS/CF units string (mutually exclusive with `--to-standard`).
- `--to-standard` — convert recognized temp/precip vars to standard display units.
- `--variable`, `-v` — restrict to a named data var. Omit with `--to-units` only when
  the store has a single data var; omit with `--to-standard` to convert all recognized
  temp/precip vars.
- `--standard-name` — override output CF `standard_name` (only with `--to-units`).

### Provenance

Appends a `{skill, version, args, input}` entry to `weather_skills_history`
(see the `provenance` skill).

## Examples

```bash
# Flux → daily depth rate
uv run --script ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i /tmp/gfs.zarr -o /tmp/gfs_mm.zarr \
    --variable precipitation_surface --to-units 'mm day-1'

# Normalize everything recognized to standard display units
uv run --script ${CLAUDE_SKILL_DIR}/scripts/unit-convert.py -i /tmp/raw.zarr -o /tmp/std.zarr \
    --to-standard
```
