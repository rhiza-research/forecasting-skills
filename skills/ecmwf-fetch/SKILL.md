---
name: ecmwf-fetch
description: Fetch an ECMWF S2S precipitation forecast (control + perturbed ensemble) for a date and region, writing a Rhiza Envelope Zarr. Use when a task needs raw S2S forecast precipitation for downstream aggregation, clipping, downscaling, or plotting.
license: MIT
compatibility: Requires Python 3.10+ and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). Requires ECMWF_API_URL, ECMWF_API_KEY, ECMWF_API_EMAIL in the environment.
---

# ecmwf-fetch

Retrieves S2S total precipitation (MARS param 228228) from the ECMWF archive, concatenates the control forecast (`cf`, number=0) and perturbed ensemble (`pf`, numbers 1..100) along the `number` dimension, and writes a consolidated Zarr store.

## When to use

- A task asks for a fresh ECMWF S2S forecast for a specific init date.
- A downstream skill needs the forecast as a Rhiza Envelope Zarr (not raw GRIB).

Not for reanalysis, climatology, or deterministic HRES — this skill is S2S only.

## Usage

```
uv run scripts/fetch.py --date YYYY-MM-DD --region <region> --output <path.zarr>
```

### Arguments
- `--date` — forecast init date (ISO).
- `--region` — one of: `africa`, `kenya`, `ghana`, `senegal`, `ethiopia`. For an explicit bbox, use `--area N/W/S/E` instead.
- `--area` — optional; `N/W/S/E` decimal degrees. Overrides `--region` if both are given.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A Zarr store with data variable `tp` (total precipitation, m) and dims `(number, step, latitude, longitude)`. `number=0` is the control; `number=1..100` are perturbed members. Stamped with `rhiza_source=ecmwf-s2s`.

## Examples

```bash
uv run scripts/fetch.py --date 2026-02-15 --region africa --output /tmp/ecmwf.zarr
```

```bash
uv run scripts/fetch.py --date 2026-02-15 --area 7/32/-6/43 --output /tmp/ecmwf_kenya.zarr
```

See [references/REFERENCE.md](references/REFERENCE.md) for the exact MARS request parameters and how retrieval time scales with area.
