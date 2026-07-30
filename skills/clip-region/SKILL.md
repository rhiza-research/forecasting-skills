---
name: clip-region
description: Spatially subset a weather-skills standard dataset to an explicit lat/lon bbox or a GeoJSON polygon. Use when you need to restrict any dataset (forecast, satellite, reanalysis, stations) before downstream aggregation or plotting. To clip to a country, get a bbox or --geojson polygon from the resolve-region skill.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/clip.py *)
metadata:
  version: "0.1.11"
  catalog-group: transforms
---

# clip-region

Source-agnostic spatial subset via `--bbox` (lat/lon slice) or `--geojson` (polygon clip). Exactly one is required. Country polygons come from `resolve-region --geojson`.

## When to use

- Narrowing a continental grid down to one country for plotting or per-country reporting.
- Applying a custom bbox or polygon to any gridded or station dataset before further processing.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/clip.py --input <in.zarr> --output <out.zarr> \
    (--bbox N/W/S/E | --geojson PATH [--keep-outside])
```

### Arguments
- `--input`, `-i` — input Zarr (gridded or station).
- `--output`, `-o` — output Zarr.
- `--bbox` — `N/W/S/E` in decimal degrees (mutually exclusive with `--geojson`).
- `--geojson` — path to a GeoJSON polygon/multipolygon; cells/stations outside are dropped (or NaN'd with `--keep-outside`).
- `--keep-outside` — with `--geojson`, keep the full grid/station set and NaN outside the polygon.

### Longitude convention

Longitudes in `[0, 360]` are auto-wrapped to `[-180, 180]` before slicing, so a global grid stored in the `[0, 360]` convention still intersects bboxes that straddle the prime meridian (e.g. Ghana). Inputs already in `[-180, 180]` pass through unchanged.

### Output

Same dims and variables, reduced to the requested window.

### Provenance

Appends a `{skill, version, args, input}` entry to `weather_skills_history`
(see the `provenance` skill). Cache keys include input basename and upstream history (no content hash).


## Example

```bash
# Get BBOX from the resolve-region skill for your country (e.g. KEN → 5.5/33.9/-4.7/41.9):
BBOX=5.5/33.9/-4.7/41.9
uv run --script ${CLAUDE_SKILL_DIR}/scripts/clip.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_kenya.zarr --bbox "$BBOX"
```
