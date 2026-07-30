---
name: ecmwf-fetch
description: Fetch an ECMWF S2S precipitation forecast (control + perturbed ensemble) for a date and bbox from the ECMWF Data Stores (ECDS), writing a weather-skills standard dataset. Use when a task needs raw S2S forecast precipitation for downstream aggregation, clipping, downscaling, or plotting. To fetch over a country, get its bbox from the resolve-region skill first.
license: MIT
compatibility: Requires Python 3.12 and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). Requires ECMWF_DATASTORES_URL and ECMWF_DATASTORES_KEY in the environment (or a `~/.ecmwfdatastoresrc` file). The URL is `https://ecds.ecmwf.int/api`; the key is the personal token from your ECDS account.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.12"
  catalog-group: fetchers
  openclaw:
    requires:
      env:
        - ECMWF_DATASTORES_URL
        - ECMWF_DATASTORES_KEY
    primaryEnv: ECMWF_DATASTORES_KEY
---

# ecmwf-fetch

Retrieves S2S total precipitation from the ECMWF Data Store (ECDS) `s2s-forecasts` collection via `ecmwf-datastores-client`. Submits the control and perturbed retrievals in parallel, concatenates the control forecast (`number=0`) and perturbed ensemble (`number=1..100`) along the `number` dimension, and writes a consolidated Zarr store.

## When to use

- A task asks for a fresh ECMWF S2S forecast for a specific init date.
- A downstream skill needs the forecast as a weather-skills standard dataset (not raw GRIB).

Not for reanalysis, climatology, or deterministic HRES — this skill is S2S only.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E --output <path.zarr>
```

### Arguments
- `--date` — forecast init date. `YYYY-MM-DD` or `latest` (newest accessible S2S init (skips embargoed inits via ECDS probe)). See CONVENTIONS date grammar.

  **`latest` cost:** resolving `latest` probes ECDS init dates backward (real retrieval submits) until one succeeds, skipping S2S real-time embargoed inits. Prefer an explicit init date when you know it. The cache key records the resolved absolute init, never the token `latest`.
- `--bbox` — required; `N/W/S/E` decimal degrees. The retrieval area (smaller bbox = faster retrieval). To fetch over a country, get its bbox from the `resolve-region` skill and pass the value here.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A Zarr store with data variable `tp` (total precipitation, `kg m⁻²` — numerically equivalent to mm depth over the accumulation period) and dims `(number, step, latitude, longitude)`. `number=0` is the control; `number=1..100` are perturbed members. Stamped with `source=ecmwf-s2s`.

### Provenance

Appends a `{skill, version, args, input}` entry to `weather_skills_history`
(see the `provenance` skill). Cache keys include input basename and upstream history (no content hash).


## Examples

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 23/-20/-37/59 --output /tmp/ecmwf.zarr
```

```bash
# Fetch over a country: get its bbox from the resolve-region skill (e.g. KEN → 5.5/33.9/-4.7/41.9)
BBOX=5.5/33.9/-4.7/41.9
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox "$BBOX" --output /tmp/ecmwf_kenya.zarr
```

```bash
# Newest available init (slow: probes init dates backward via ECDS submits)
# (bbox from the resolve-region skill, e.g. KEN)
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date latest --bbox 5.5/33.9/-4.7/41.9 \
    --output /tmp/ecmwf_latest.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the exact ECDS request parameters and how retrieval time scales with area.
