---
name: downscale
description: Spatially coarsen a Rhiza Envelope Zarr by an integer factor or to a target resolution, using a choice of aggregation methods. Use when a task needs to reduce the spatial resolution of any gridded dataset (forecast, satellite, reanalysis) to match another grid or to speed up downstream steps.
compatibility: Requires Python 3.10+ and uv.
---

# downscale

Source-agnostic spatial coarsening: aggregates `factor x factor` cells (or cells covering `target-resolution` degrees) along the detected latitude/longitude dims using a caller-chosen reducer. Works on any gridded Rhiza Envelope Zarr regardless of source.

## When to use

- A gridded Zarr needs to be brought onto a coarser grid before plotting, comparison, or ensemble aggregation.
- Matching the resolution of another dataset (e.g. IMERG 0.1° onto the CHIRPS 0.05° grid coarsened to 0.25°).

Not for: statistical/bias-corrected downscaling to *higher* resolution — that's a domain-specific operation and not this skill.

## Usage

```
uv run scripts/downscale.py --input <in.zarr> --output <out.zarr> \
    (--factor N | --target-resolution DEG) \
    [--method mean|sum|max|min|median] \
    [--boundary trim|pad] [--skipna | --no-skipna] \
    [--dims LAT,LON] [--variable NAME]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded envelope).
- `--output`, `-o` — output Zarr.
- `--factor`, `-f` — integer coarsening factor (>= 2). Mutually exclusive with `--target-resolution`.
- `--target-resolution` — target spacing in degrees; factor is derived from the input grid spacing.
- `--method` — `mean` (default), `sum`, `max`, `min`, `median`. Pick `sum` for flux-like variables (precip totals), `mean` for state variables (temperature).
- `--boundary` — `trim` (default) drops partial edge cells; `pad` keeps them with NaN padding.
- `--skipna` / `--no-skipna` — NaN handling. Default `--skipna` (ignore NaNs when aggregating), appropriate for sparse satellite/station grids.
- `--dims` — comma-separated lat,lon dim names. Defaults autodetect among `latitude/lat/y` and `longitude/lon/x`.
- `--variable` — restrict to a single data variable. Default: coarsen all.

### Output

Same shape as input except the lat/lon dims are smaller. Non-spatial dims (`number`, `step`, `time`) are preserved. `rhiza_downscale_factor` attr is stamped.

## Examples

```bash
uv run scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_10x.zarr \
    --factor 10 --method mean
```

```bash
uv run scripts/downscale.py -i /tmp/imerg.zarr -o /tmp/imerg_p25.zarr \
    --target-resolution 0.25 --method sum
```
