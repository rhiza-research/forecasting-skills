---
name: downscale-coarsen
description: Spatially coarsen a Rhiza Envelope Zarr by an integer FACTOR (e.g. 10× coarser) or to a TARGET RESOLUTION in degrees (e.g. ~0.25°), using a choice of aggregation methods. Use when you want "N times coarser than what I have" without picking a named target grid. For grid-to-grid regridding (e.g. global0_25 → global1_5), use the sheerwater `regrid` skill instead.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  openclaw:
    requires:
      bins:
        - uv
---

# downscale-coarsen

Source-agnostic spatial coarsening: aggregates `factor x factor` cells (or cells covering `target-resolution` degrees) along the detected latitude/longitude dims using a caller-chosen reducer. Works on any gridded Rhiza Envelope Zarr regardless of source.

## How this differs from sheerwater's `regrid`

Sheerwater has a `regrid` skill that targets named grids (`global0_25`, `global1_5`, `global0_1`, etc.) using xarray-regrid with linear / nearest / cubic / conservative / most-common methods. That's the right pick when your goal is "land on the same grid as some other dataset I'm comparing against".

This skill takes a different input: an integer factor or an approximate target spacing in degrees. It uses `xarray.coarsen` (not regridding), so the output cell centers are simple multiples of the input cell centers — no interpolation between grids. That's the right pick when your goal is "make this 10× coarser, I don't care which named grid I land on".

If both skills are installed, route by what you specify:

- "I want grid `global1_5`" → sheerwater `regrid`.
- "I want this conservatively regridded onto a known grid" → sheerwater `regrid`.
- "I want a factor of 10× coarser" or "approximately 0.25°" without picking a named grid → this skill.

When sheerwater adds coarsen-by-factor as a sibling (see TODO in `sheerwater.utils.data_utils.regrid`), this skill becomes redundant and will be removed.

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
