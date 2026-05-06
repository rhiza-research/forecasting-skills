---
name: downscale
description: Spatially regrid a Rhiza Envelope Zarr onto a coarser grid using linear interpolation, by an integer factor or to a target resolution. Use when a task needs to reduce the spatial resolution of any gridded dataset (forecast, satellite, reanalysis) to match another grid or to speed up downstream steps.
license: MIT
compatibility: Requires Python 3.10+ and uv.
---

# downscale

Source-agnostic spatial regridding: linearly interpolates the input onto a uniform target grid `factor` times coarser (or with spacing `target-resolution` degrees) along the detected latitude/longitude dims. Works on any gridded Rhiza Envelope Zarr regardless of source. Optionally applies empirical quantile-quantile mapping after regridding to bias-correct the regridded values against a reference distribution.

## When to use

- A gridded Zarr needs to be brought onto a coarser grid before plotting, comparison, or ensemble aggregation.
- Matching the resolution of another dataset (e.g. IMERG 0.1° onto a 0.25° grid).
- Bias-correcting the regridded output against an observational reference on the same target grid (opt-in via `--qq-reference`).

Not for: statistical/bias-corrected downscaling to *higher* resolution — that's a domain-specific operation and not this skill.

## Usage

```
uv run scripts/downscale.py --input <in.zarr> --output <out.zarr> \
    (--factor N | --target-resolution DEG) \
    [--dims LAT,LON] [--variable NAME] \
    [--qq-reference REF.zarr] [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded envelope).
- `--output`, `-o` — output Zarr.
- `--factor`, `-f` — integer coarsening factor (>= 2). Mutually exclusive with `--target-resolution`. Target spacing = `factor` × input spacing.
- `--target-resolution` — target spacing in degrees; factor is derived from the input grid spacing.
- `--dims` — comma-separated lat,lon dim names. Defaults autodetect among `latitude/lat/y` and `longitude/lon/x`.
- `--variable`, `-v` — restrict to a single data variable. Default: regrid all.
- `--qq-reference` — optional path to a reference Zarr. When given, applies per-grid-cell empirical quantile mapping along `--time-dim` after the regrid, mapping the regridded values to the reference distribution. The reference must already be on the post-regrid lat/lon grid; mismatches are an error.
- `--time-dim` — time dimension used as the sample axis for q-q mapping. Default: `time`. Both the input and the reference must have a dimension by this name.

### Output

Same shape as input except the lat/lon dims are smaller. Non-spatial dims (`number`, `step`, `time`) are preserved. `rhiza_downscale_factor` and `rhiza_downscale_method=linear` attrs are stamped. When `--qq-reference` is used, `rhiza_downscale_qq_method=empirical` and `rhiza_downscale_qq_reference=<path>` attrs are also stamped, and only data variables present in both the regridded output and the reference are mapped (others pass through unchanged).

## Examples

```bash
uv run scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_10x.zarr --factor 10
```

```bash
uv run scripts/downscale.py -i /tmp/imerg.zarr -o /tmp/imerg_p25.zarr --target-resolution 0.25
```

Q-Q map ECMWF onto a 0.25° grid using ERA5 observations on the same grid as the post-regrid reference distribution:

```bash
uv run scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_p25_qq.zarr \
    --target-resolution 0.25 --qq-reference /tmp/era5_p25.zarr
```
