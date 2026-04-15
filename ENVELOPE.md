# Rhiza Envelope

The common Zarr-based container that the skills in this repo consume and produce.

## Shape

A Zarr v2 store containing one or more data variables.

### Gridded envelope
- Spatial dims: `latitude`, `longitude` (aliases `lat`/`lon`, `y`/`x` also accepted on input).
- Temporal dims: exactly one of
  - `time` — observations, a wall-clock timestamp per slice.
  - `step` (forecast lead time, `timedelta64`) plus a scalar `time` coord for the forecast init date.
- Optional `number` — ensemble member index (control = 0; perturbed members 1..N).
- Optional other dims (e.g. `level`) are preserved by middle-of-pipeline skills and ignored when unused.

### Station envelope
- Single spatial dim `station_id` (string).
- 1-D coords `latitude(station_id)` and `longitude(station_id)`.
- `time` dim as above.

## Attrs

Skills may stamp these on the Zarr root for traceability. None are required for correctness.

| Attr | Set by | Meaning |
|---|---|---|
| `rhiza_source` | fetchers | e.g. `ecmwf-s2s`, `chirps`, `imerg`, `tahmo` |
| `rhiza_region` | `ecmwf-fetch`, `clip-region` | Last named region or bbox applied |
| `rhiza_date` | fetchers | Primary temporal anchor (ISO) |
| `rhiza_downscale_factor` | `downscale` | Integer factor if coarsen was applied |
| `rhiza_aggregation` | `aggregate-temporal` | e.g. `dekadal-sum`, `weekly-mean` |

## Conventions

- Spatial and time coords should carry CF `standard_name`, `units`, and `axis` attrs (`latitude`/`degrees_north`/`Y`, `longitude`/`degrees_east`/`X`, `time`/—/`T`). Fetchers stamp these on write; generic middle skills use [cf-xarray](https://cf-xarray.readthedocs.io/) to identify coords via those attrs and fall back to name heuristics (`lat`/`lon`/`y`/`x`) when attrs are missing.
- Data variable units should follow CF where possible (`m` for precipitation, `K` or `degC` for temperature).
- Output stores are written with `consolidated=True`.
- Missing data is encoded as NaN, not a sentinel value.
- **Per-variable `encoding` (codecs, chunks, dtype, fill_value) is NOT part of the envelope contract.** Each skill writes with its own `zarr`/`numcodecs` versions and the codec objects are not guaranteed to be round-trippable across skill boundaries. Skills that read a Zarr and re-write must clear `.encoding = {}` on every variable before calling `to_zarr()`; fetchers should do the same on the way out. Consumers rely only on dims, coords, data-variable names, values, and `rhiza_*` attrs.
