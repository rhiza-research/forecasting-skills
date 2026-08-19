---
name: ecmwf-fetch
description: Fetch an ECMWF S2S precipitation forecast (control + perturbed ensemble) for a date and bbox from the ECMWF Data Stores (ECDS), writing a weather-skills standard dataset Zarr. Use when a task needs raw S2S forecast precipitation for downstream aggregation, clipping, downscaling, or plotting. To fetch over a country, get its bbox from the resolve-region skill first.
license: MIT
compatibility: Requires Python 3.12 and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). Requires ECMWF_DATASTORES_URL and ECMWF_DATASTORES_KEY in the environment (or a `~/.ecmwfdatastoresrc` file). The URL is `https://ecds.ecmwf.int/api`; the key is the personal token from your ECDS account.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
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
- A downstream skill needs the forecast as a weather-skills standard dataset Zarr (not raw GRIB).

Not for reanalysis, climatology, or deterministic HRES — this skill is S2S only.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E --output <path.zarr>
```

### Arguments
- `--date` — forecast init date. Absolute ISO date `YYYY-MM-DD`. Real-time
  ECMWF S2S has run **daily** (00 UTC) since IFS Cycle 48r1 (2023-06-27);
  before that it was Mondays and Thursdays only. Requesting a date with no
  published init exits non-zero with a clear "no data for this init" message.
  Recent ECMWF S2S real-time data is access-restricted (embargoed) for a
  variable window; if the requested init falls inside the embargo, the error
  says so explicitly and suggests an older init date. Transport and auth
  failures are surfaced as clear errors — not raw tracebacks.
- `--bbox` — required; `N/W/S/E` decimal degrees. The retrieval area (smaller bbox = faster retrieval). To fetch over a country, get its bbox from the `resolve-region` skill and pass the value here.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A Zarr store with data variable `tp` (total precipitation amount, `mm`) and dims `(number, step, latitude, longitude)`. `number=0` is the control; `number=1..100` are perturbed members. Stamped with `weather_skills_source=ecmwf-s2s`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
records the run's flag values under underscored names (e.g. a flag
`--time-dim` is recorded as `time_dim`); `version` is the value printed by
`--help`. Inspect a written output's provenance with the `provenance` skill.

## Examples

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 23/-20/-37/59 --output /tmp/ecmwf.zarr
```

```bash
# Fetch over a country: get its bbox from the resolve-region skill (e.g. KEN → 5.5/33.9/-4.7/41.9)
BBOX=5.5/33.9/-4.7/41.9
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox "$BBOX" --output /tmp/ecmwf_kenya.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the exact ECDS request parameters and how retrieval time scales with area.
