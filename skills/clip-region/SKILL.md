---
name: clip-region
description: Spatially subset a gridded Rhiza Envelope Zarr to an explicit lat/lon bbox. Use when you need to restrict any dataset (forecast, satellite, reanalysis) to a custom bounding box before downstream aggregation or plotting. To clip to a country, get its bbox from the resolve-region skill first.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.5"
---

# clip-region

Source-agnostic spatial subset using simple lat/lon slicing, driven by an explicit `--bbox`. To clip to a country, resolve its bbox with the `resolve-region` skill and pass that value.

## When to use

- Narrowing a continental grid down to one country for plotting or per-country reporting.
- Applying a custom bbox to any gridded envelope before further processing.

Does **not** clip station-schema envelopes (station_id-indexed). For stations, filter by country using an `aggregate-*` or custom skill.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py --input <in.zarr> --output <out.zarr> \
    --bbox N/W/S/E [--dims LAT,LON]
```

### Arguments
- `--input`, `-i` — gridded Zarr.
- `--output`, `-o` — output Zarr.
- `--bbox` — required; `N/W/S/E` in decimal degrees. To clip to a country, get its bbox from the `resolve-region` skill and pass the value here.
- `--dims` — optional `LAT,LON` dim name override.

### Longitude convention

Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before slicing, so a global grid stored in the `[0, 360]` convention still intersects bboxes that straddle the prime meridian (e.g. Ghana). Inputs already in `[-180, 180]` pass through unchanged.

### Output

Same dims and variables, reduced to the requested window.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `rhiza_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes, so a renamed-but-unchanged input still cache-hits and a
same-named-but-modified input correctly cache-misses; `version` is the
`_RHIZA_SKILL_VERSION` constant in `scripts/clip.py`, kept in lockstep with
`metadata.version` in this SKILL.md by the CI version-bump workflow.
Cache-hit comparison reads the existing output's
`rhiza_history`: a hit requires the upstream entries to match and the last
entry's `skill`, `args`, and `input` to match the proposed new entry.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `anchor_end`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--anchor-end`). A consumer
reconstructing a `uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Example

```bash
# Get BBOX from the resolve-region skill for your country (e.g. KEN → 5.5/33.9/-4.7/41.9):
BBOX=5.5/33.9/-4.7/41.9
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_kenya.zarr --bbox "$BBOX"
```
