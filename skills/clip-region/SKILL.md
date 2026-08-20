---
name: clip-region
description: Spatially subset a weather-skills standard dataset Zarr to a lat/lon bbox or GeoJSON polygon. Use when you need to restrict any dataset (forecast, satellite, reanalysis, stations) before downstream aggregation or plotting. Named places: get a bbox (or polygon) from the resolve-region skill first.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py *)
metadata:
  catalog-group: transforms
---

# clip-region

Source-agnostic spatial subset. Pass an explicit `--bbox` or a `--geojson`
polygon. For a named country or county, run the `resolve-region` skill first
and pass its printed bbox (or the GeoJSON file it writes).

## When to use

- Narrowing a continental grid down to one country for plotting or per-country reporting.
- Clipping a gridded or station dataset to a custom polygon.
- Applying a custom bbox to any gridded dataset before further processing.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py --input <in.zarr> --output <out.zarr> \
    --bbox N/W/S/E

uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py --input <in.zarr> --output <out.zarr> \
    --geojson boundary.geojson [--keep-outside]
```

### Arguments
- `--input`, `-i` — input Zarr (gridded/spatial or point_obs).
- `--output`, `-o` — output Zarr.
- `--bbox` — `N/W/S/E` in decimal degrees. Mutex with `--geojson`. Named
  places: compose with the `resolve-region` skill and pass the printed value.
- `--geojson` — path to a GeoJSON Feature/FeatureCollection/geometry. Mutex
  with `--bbox`. For a country or county polygon, write it with
  `resolve-region --geojson`.
- `--keep-outside` — with `--geojson` only: set values outside the polygon to NaN instead of dropping cells/stations.

### Longitude convention

Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before clipping, so a global grid stored in the `[0, 360]` convention still intersects bboxes/polygons that use negative lon.

### Output

A spatial subset of the input Zarr with provenance history stamped.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `weather_skills_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes; `version` is the `_SKILL_VERSION` constant in `scripts/clip.py`.

The `args` dict stores argparse dest names (underscored), not the hyphenated CLI
flag names. A consumer reconstructing a
`uv run ${CLAUDE_SKILL_DIR}/scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Example

```bash
# Named places: run resolve-region first, then pass the printed N/W/S/E:
uv run ${CLAUDE_SKILL_DIR}/scripts/clip.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_kenya.zarr --bbox 5/34/-5/42
```
