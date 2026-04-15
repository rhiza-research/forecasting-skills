---
name: clip-region
description: Spatially subset a gridded Rhiza Envelope Zarr to a named region or explicit lat/lon bbox. Use when you need to restrict any dataset (forecast, satellite, reanalysis) to a country or custom bounding box before downstream aggregation or plotting.
compatibility: Requires Python 3.10+ and uv.
---

# clip-region

Source-agnostic spatial subset using simple lat/lon slicing. Accepts either a named region (Africa countries used in the daily S2S workflow) or an explicit `--bbox`.

## When to use

- Narrowing a continental grid down to one country for plotting or per-country reporting.
- Applying a custom bbox to any gridded envelope before further processing.

Does **not** clip station-schema envelopes (station_id-indexed). For stations, filter by country using an `aggregate-*` or custom skill.

## Usage

```
uv run scripts/clip.py --input <in.zarr> --output <out.zarr> \
    (--region NAME | --bbox N/W/S/E) [--dims LAT,LON]
```

### Arguments
- `--input`, `-i` — gridded Zarr.
- `--output`, `-o` — output Zarr.
- `--region` — named region: `africa`, `kenya`, `ghana`, `senegal`, `ethiopia`, `namibia`, `botswana`, `zambia`, `madagascar`, `angola`.
- `--bbox` — explicit `N/W/S/E` in decimal degrees (overrides `--region` if both given).
- `--dims` — optional `LAT,LON` dim name override.

### Output

Same dims and variables, reduced to the requested window. Stamps `rhiza_region` on the output.

## Example

```bash
uv run scripts/clip.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_kenya.zarr --region kenya
```
