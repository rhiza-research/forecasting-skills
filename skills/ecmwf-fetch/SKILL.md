---
name: ecmwf-fetch
description: Fetch an ECMWF S2S ensemble forecast (control + perturbed) for a date and bbox from the ECMWF Data Stores (ECDS), writing a weather-skills standard dataset Zarr. Default `-v tp`. Most used: `tp`, `t2m` (then `d2m`, `mx2t6`/`mn2t6`, `u10`/`v10`). `-v` is the short name (`t2m`), not ARCO `2m_temperature` or ECDS `2_m_temperature`. Real-time S2S has a 2-day embargo — request an init at least 2 days old. Fetch writes `tp` as a per-step rate (`mm day-1`) and temperatures as `degree_Celsius` — do not run deaccumulate after this skill. To fetch over a country, get its bbox from the resolve-region skill first.
license: MIT
compatibility: Requires Python 3.12 and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). Requires ECMWF_DATASTORES_URL and ECMWF_DATASTORES_KEY in the environment (or a `~/.ecmwfdatastoresrc` file). The URL is `https://ecds.ecmwf.int/api`; the key is the personal token from your ECDS account.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: fetchers
  availability:
    shape: date
    policy: embargo
    schedule: ecmwf-s2s
    earliest: 2015-01-01
    note: ECMWF S2S real-time 2-day embargo; daily inits since 2023-06-27
  variables:
    - tp
    - t2m
    - d2m
    - mx2t6
    - mn2t6
    - u10
    - v10
    - msl
    - cape
    - tcw
  openclaw:
    requires:
      env:
        - ECMWF_DATASTORES_URL
        - ECMWF_DATASTORES_KEY
    primaryEnv: ECMWF_DATASTORES_KEY
---

# ecmwf-fetch

Retrieves ECMWF S2S single-level fields from the ECMWF Data Store (ECDS)
`s2s-forecasts` collection via `ecmwf-datastores-client`. Submits the control
and perturbed retrievals in parallel, concatenates the control forecast
(`number=0`) and perturbed ensemble (`number=1..100`) along the `number`
dimension, and writes a consolidated Zarr store. Default field is `tp`.

## When to use

- A task asks for an ECMWF S2S forecast for a specific init date (real-time
  inits are embargoed for 2 days).
- A downstream skill needs the forecast as a weather-skills standard dataset
  Zarr (not raw GRIB).

Not for reanalysis, climatology, or deterministic HRES — this skill is S2S only.
S2S has many more parameters (soil, ocean, pressure levels); this skill only
fetches the usual surface fields below.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E [-v VAR ...] --output <path.zarr>
```

### Arguments
- `--date` — forecast init date. Absolute ISO date `YYYY-MM-DD`. Real-time
  ECMWF S2S has run **daily** (00 UTC) since IFS Cycle 48r1 (2023-06-27);
  before that it was Mondays and Thursdays only. Requesting a date with no
  published init exits non-zero with a clear "no data for this init" message.
  Recent ECMWF S2S real-time data is access-restricted (embargoed) for **2
  days**; request an init at least 2 days old. If the requested init falls
  inside the embargo, the error says so explicitly and suggests an older init
  date. Transport and auth failures are surfaced as clear errors — not raw
  tracebacks.
- `--bbox` — required; `N/W/S/E` decimal degrees. The retrieval area (smaller bbox = faster retrieval). To fetch over a country, get its bbox from the `resolve-region` skill and pass the value here.
- `--variable`, `-v` — S2S field to retrieve (repeatable). Default `tp`.
  Unknown names exit non-zero and print `Available (most used first):`. Use
  the short names in the table — not ARCO `2m_temperature` / `total_precipitation`
  and not the ECDS form spelling `2_m_temperature`.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Variables (most used first)

| `-v` | Field | Notes |
|---|---|---|
| `tp` | Total precipitation | **Default.** Written as a per-step rate (`mm day-1`). Aggregate then `convert-to-totals` for period `mm`. |
| `t2m` | 2 m temperature | Daily mean, `degree_Celsius`. Prefer this for "how warm". |
| `d2m` | 2 m dewpoint temperature | Daily mean. |
| `mx2t6` / `mn2t6` | Max / min 2 m temperature in the last 6 hours | |
| `u10` / `v10` | 10 m wind components | |
| `msl` | Mean sea-level pressure | |
| `cape` | Convective available potential energy | Daily mean. |
| `tcw` | Total column water | Daily mean. |

Daily-mean fields (`t2m`, `d2m`, `cape`, `tcw`) use ECDS 24-hour leadtime
windows; the others use the same instant/accum lead hours as `tp`. Mixing the
two groups (e.g. `-v tp -v t2m`) submits extra retrieval legs.

### Output

A Zarr store with the selected data variables and dims `(number, step, latitude, longitude)`. `number=0` is the control; `number=1..100` are perturbed members. `tp` is a precipitation **rate** (`mm day-1`); known temperature fields are `degree_Celsius`. Stamped with `weather_skills_source=ecmwf-s2s`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
records the run's flag values under underscored names (e.g. a flag
`--time-dim` is recorded as `time_dim`); `version` is the value printed by
`--help`. Inspect a written output's provenance with the `provenance` skill.

## Examples

```bash
# Default: total precipitation, continental Africa (custom bbox — not a country lookup)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 23/-20/-37/59 --output /tmp/ecmwf.zarr
```

```bash
# 2 m temperature (the other most-used S2S field)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 -v t2m --output /tmp/ecmwf_t2m.zarr
```

```bash
# Named country: run resolve-region first, then pass the printed N/W/S/E:
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 --output /tmp/ecmwf_kenya.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the exact ECDS request parameters and how retrieval time scales with area.
