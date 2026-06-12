# Weather-Skills Envelope

The common Zarr-based container that the skills in this repo consume and produce.

## Shape

A Zarr v3 store containing one or more data variables. Consumers also read Zarr v2 stores (xarray detects the format on open).

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

`weather_skills_source` is optional metadata for human readability. `weather_skills_history` is the canonical provenance chain and is set by every zarr-writing skill.

| Attr | Set by | Meaning |
|---|---|---|
| `weather_skills_source` | fetchers | e.g. `ecmwf-s2s`, `chirps`, `imerg`, `tahmo` |
| `weather_skills_history` | every zarr-writing skill | JSON-encoded append-only provenance chain (see below) |

### `weather_skills_history` schema

`weather_skills_history`, when present, MUST be a JSON-encoded array, ordered oldest first along the pipeline. A consumer that reads an artifact whose `weather_skills_history` is present but not a JSON array (a JSON object, a scalar, or non-JSON text) treats it as no history: it proceeds as if the attribute were absent and prints a one-line warning to stderr pointing at `provenance --check`. The coercion is array-level only — an array whose individual entries are imperfect (missing keys, an old `version`) is passed through unchanged. `provenance --check` validates an artifact's compliance with this schema and reports per-entry violations.

Each entry is an object with these fields:

- `skill` — canonical skill name (e.g. `clip-region`).
- `version` — the SKILL.md `metadata.version` value at the time the entry was written, kept in lockstep with the script's `_SKILL_VERSION` constant by CI.
- `args` — the script's argparse namespace minus `--input`/`--output` path strings. Keys are argparse dest names (underscored).
- `input` — for fetchers, `null` (no upstream zarr); for single-input transformers, a `{basename, hash}` dict where `hash` is a sha256 over the upstream zarr's stored bytes; for multi-input transformers like `concat`, a list of `{basename, hash, history}` dicts in input order, where `history` is that input's full `weather_skills_history` chain (an empty list when the input had no `weather_skills_history`). A multi-input entry therefore records every input branch in full, while the store's top-level `weather_skills_history` stays a single linear array (the first input's chain plus the merge entry) so single-attr readers keep working.

PNG outputs from plot-writers embed the same schema in PNG `tEXt` chunks via matplotlib's `savefig(metadata=...)`. Single-input plotters use the key `weather_skills_history`; two-input plotters use a pair of keys — `weather_skills_history_a` / `weather_skills_history_b` for `plot-compare`, `weather_skills_history_forecast` / `weather_skills_history_mclimate` for `plot-mediogram` — one per input branch. Read-back via `PIL.Image.open(path).info` or `exiftool`.

## Conventions

The envelope is a **CF-compliant** Zarr store: it conforms to the [CF Conventions](https://cfconventions.org/). Producers (fetchers) MUST emit valid CF; every consumer MUST read any valid CF input — including stores whose time axis uses a non-standard model calendar (`noleap`, `360_day`). The CF spec, not a repo-specific subset of it, is the contract: this repo does not maintain its own list of required CF attributes. Canonical dimension names are listed under Shape above; generic middle skills use [cf-xarray](https://cf-xarray.readthedocs.io/) to identify coords from their CF attrs, falling back to name heuristics (`lat`/`lon`/`y`/`x`) when attrs are missing.

- Output stores are written with `consolidated=True`. Consolidated metadata is a zarr-python convenience, not part of the Zarr v3 specification: it is embedded as an extra key in the root `zarr.json`, implementations that do not support it ignore the key and read the per-node metadata, and zarr-python prints a `ZarrUserWarning` about the spec gap on every consolidated write.
- Missing data is encoded as NaN, not a sentinel value.
- **Per-variable `encoding` (codecs, chunks, dtype, fill_value) is NOT part of the envelope contract.** Each skill writes with its own `zarr`/`numcodecs` versions and the codec objects are not guaranteed to be round-trippable across skill boundaries. Skills that read a Zarr and re-write must clear `.encoding = {}` on every variable before calling `to_zarr()`; fetchers should do the same on the way out. Consumers rely only on dims, coords, data-variable names, values, and `weather_skills_*` attrs.
