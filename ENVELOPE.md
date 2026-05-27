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

`rhiza_source` is optional metadata for human readability. `rhiza_history` is the canonical provenance chain and is set by every zarr-writing skill.

| Attr | Set by | Meaning |
|---|---|---|
| `rhiza_source` | fetchers | e.g. `ecmwf-s2s`, `chirps`, `imerg`, `tahmo` |
| `rhiza_history` | every zarr-writing skill | JSON-encoded append-only provenance chain (see below) |

### `rhiza_history` schema

A JSON-encoded array, ordered oldest first along the pipeline. Each entry is an object with these fields:

- `skill` — canonical skill name (e.g. `clip-region`).
- `version` — the SKILL.md `metadata.version` value at the time the entry was written, kept in lockstep with the script's `_RHIZA_SKILL_VERSION` constant by CI.
- `args` — the script's argparse namespace minus `--input`/`--output` path strings. Keys are argparse dest names (underscored).
- `input` — for fetchers, `null` (no upstream zarr); for single-input transformers, a `{basename, hash}` dict where `hash` is a sha256 over the upstream zarr's stored bytes; for multi-input transformers like `concat`, a list of `{basename, hash, history}` dicts in input order, where `history` is that input's full `rhiza_history` chain (an empty list when the input had no `rhiza_history`). A multi-input entry therefore records every input branch in full, while the store's top-level `rhiza_history` stays a single linear array (the first input's chain plus the merge entry) so single-attr readers keep working.

PNG outputs from plot-writers embed the same schema in PNG `tEXt` chunks via matplotlib's `savefig(metadata=...)`. Single-input plotters use the key `rhiza_history`; two-input plotters use a pair of keys — `rhiza_history_a` / `rhiza_history_b` for `plot-compare`, `rhiza_history_forecast` / `rhiza_history_mclimate` for `plot-mediogram` — one per input branch. Read-back via `PIL.Image.open(path).info` or `exiftool`.

## Conventions

- Spatial and time coords should carry CF `standard_name`, `units`, and `axis` attrs (`latitude`/`degrees_north`/`Y`, `longitude`/`degrees_east`/`X`, `time`/—/`T`). Fetchers stamp these on write; generic middle skills use [cf-xarray](https://cf-xarray.readthedocs.io/) to identify coords via those attrs and fall back to name heuristics (`lat`/`lon`/`y`/`x`) when attrs are missing.
- Data variable units should follow CF where possible (`m` for precipitation, `K` or `degC` for temperature).
- Output stores are written with `consolidated=True`.
- Missing data is encoded as NaN, not a sentinel value.
- **Per-variable `encoding` (codecs, chunks, dtype, fill_value) is NOT part of the envelope contract.** Each skill writes with its own `zarr`/`numcodecs` versions and the codec objects are not guaranteed to be round-trippable across skill boundaries. Skills that read a Zarr and re-write must clear `.encoding = {}` on every variable before calling `to_zarr()`; fetchers should do the same on the way out. Consumers rely only on dims, coords, data-variable names, values, and `rhiza_*` attrs.
