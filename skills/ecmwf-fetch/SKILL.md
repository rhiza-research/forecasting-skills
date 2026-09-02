---
name: ecmwf-fetch
description: "On the first call, inject secrets ECMWF_DATASTORES_URL and ECMWF_DATASTORES_KEY as environment variables — do not run once without them and retry. Prefer dynamical-fetch for medium-range ECMWF IFS-ENS / AIFS (credential-free). This skill fetches an ECMWF S2S ensemble forecast (control + perturbed) for a date and bbox from the ECMWF Data Stores (ECDS), writing a weather-skills standard dataset Zarr. Default `-v tp`. Pass several fields in one call (`-v tp t2m` or `-v tp -v t2m`). Most used: `tp`, `t2m`, `sst` (then `d2m`, winds). Pressure-level: `-v t` / `-v gh` (control only, all native levels). `-v` is the short name (`t2m`, `sst`, `t`), not ARCO `2m_temperature`. Real-time S2S has a 2-day embargo — request an init at least 2 days old. Fetch writes `tp` as a per-step rate (`mm day-1`) and temperatures as `degree_Celsius` — do not run deaccumulate after this skill. To fetch over a country, get its bbox from the resolve-region skill first."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). Requires ECMWF_DATASTORES_URL and ECMWF_DATASTORES_KEY in the environment (or a `~/.ecmwfdatastoresrc` file). The URL is `https://ecds.ecmwf.int/api`; the key is the personal token from your ECDS account.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - sst
    - d2m
    - mx2t6
    - mn2t6
    - u10
    - v10
    - msl
    - cape
    - tcw
    - t
    - gh
  openclaw:
    requires:
      env:
        - ECMWF_DATASTORES_URL
        - ECMWF_DATASTORES_KEY
    primaryEnv: ECMWF_DATASTORES_KEY
    envVars:
      - name: ECMWF_DATASTORES_URL
        description: ECDS API base URL (https://ecds.ecmwf.int/api)
      - name: ECMWF_DATASTORES_KEY
        description: Personal ECDS token from your ECMWF Data Stores account
---

# ecmwf-fetch

Retrieves ECMWF S2S single-level fields from the ECMWF Data Store (ECDS)
`s2s-forecasts` collection via `ecmwf-datastores-client`. Submits the control
and perturbed retrievals in parallel, concatenates the control forecast
(`number=0`) and perturbed ensemble (`number=1..100`) along the `number`
dimension, and writes a consolidated Zarr store. Default field is `tp`.

## When to use

Prefer `dynamical-fetch` (`ecmwf-ifs-ens-forecast-15-day-0-25-degree` or
`ecmwf-aifs-ens-forecast`) for medium-range ECMWF — credential-free, 0.25°,
no embargo. This skill is **S2S only**.

- A task asks for an ECMWF S2S forecast for a specific init date (real-time
  inits are embargoed for 2 days), or for S2S fields the dynamical catalog
  does not carry (ocean, full pressure-level stack, 46-day leads).
- A downstream skill needs the forecast as a weather-skills standard dataset
  Zarr (not raw GRIB).

Not for reanalysis, climatology, or deterministic HRES. It retrieves S2S
single-level, ocean, pressure-level, and potential-vorticity fields.

## Credentials

The fetch process does not inherit host secrets. On the **first** invocation
that talks to ECDS, inject both of these as environment variables:

- `ECMWF_DATASTORES_URL` — `https://ecds.ecmwf.int/api`
- `ECMWF_DATASTORES_KEY` — personal ECDS token

Do not call the skill once to discover they are missing, then retry. `--probe-latest`
does not need credentials. Never print, log, or echo the values.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E [-v VAR ...] --output <path.zarr>
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest
```

### Arguments
- `--date` — forecast init date. Absolute ISO date `YYYY-MM-DD`. Calendar day: `resolve-time latest`. Latest published init (2-day embargo): `--probe-latest`. Real-time
  ECMWF S2S has run **daily** (00 UTC) since IFS Cycle 48r1 (2023-06-27);
  before that it was Mondays and Thursdays only. Requesting a date with no
  published init exits non-zero with a clear "no data for this init" message.
  Recent ECMWF S2S real-time data is access-restricted (embargoed) for **2
  days**. If the requested init falls inside the embargo, the error says so
  explicitly and suggests an older init date. Transport and auth failures are
  surfaced as clear errors — not raw tracebacks.
- `--probe-latest` — print the latest init expected off embargo (`YYYY-MM-DD`) on stdout and exit. No `-o`.
- `--bbox` — required; `N/W/S/E` decimal degrees. The retrieval area (smaller bbox = faster retrieval). To fetch over a country, get its bbox from the `resolve-region` skill and pass the value here.
- `--variable`, `-v` — S2S fields to retrieve. Default `tp`. Pass several in
  one call: `-v tp t2m`, `-v tp -v t2m`, or `-v tp,t2m`. Compatible fields
  share one ECDS request; mixed families (instant vs daily, or pressure-level)
  submit extra legs. Unknown names exit non-zero and print
  `Available (most used first):`. Use the short names (`sst`, `t2m`) — not
  ARCO `2m_temperature` / `total_precipitation`. ECDS form names
  (`sea_surface_temperature`, `2_m_temperature`) are also accepted.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Variables (most used first)

| `-v` | Field | Notes |
|---|---|---|
| `tp` | Total precipitation | **Default.** Written as a per-step rate (`mm day-1`). Aggregate then `convert-to-totals` for period `mm`. |
| `t2m` | 2 m temperature | Daily mean, `degree_Celsius`. Prefer this for "how warm". |
| `sst` | Sea-surface temperature | Daily mean, `degree_Celsius`. S2S GRIB short name `wtmp` is accepted. |
| `d2m` | 2 m dewpoint temperature | Daily mean. |
| `mx2t6` / `mn2t6` | Max / min 2 m temperature in the last 6 hours | |
| `u10` / `v10` | 10 m wind components | |
| `msl` | Mean sea-level pressure | |
| `cape` | Convective available potential energy | Daily mean. |
| `tcw` | Total column water | Daily mean. |
| `gh` | Geopotential height | Pressure levels 1000–10 hPa. Control forecast only. |
| `t` | Temperature on pressure levels | Same levels. Control only. `degree_Celsius`. |
| `u` / `v` / `w` | Wind / vertical velocity on pressure levels | Control only. |
| `q` | Specific humidity | 1000–200 hPa (7 levels). Control only. |
| `pv` | Potential vorticity | 320 K isentropic level. Control only. |

The skill also accepts the rest of the S2S single-level and ocean parameters
(soil moisture/temperature, snow, fluxes, runoff, sea ice, ocean currents,
…). Pass an unknown `-v` to print the full `Available:` list, or see
[references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md).
Ocean fields are on a 1.0° grid; atmosphere single-level and pressure-level
fields are 1.5° — do not mix ocean with atmosphere in one call.

Daily-mean fields (`t2m`, `sst`, `d2m`, `cape`, `tcw`, and the other daily
parameters) use ECDS 24-hour leadtime windows; instant/accumulated and
pressure-level fields use the same daily 00Z lead hours as `tp` (`0`, `24`,
… `1104` hours, the 46-day S2S range). Mixing groups (e.g. `-v tp -v t`)
submits extra retrieval legs. Pressure-level and `pv` fields are archived
with the **control forecast only** (`number=0`).

### Output

A Zarr store with the selected data variables and dims `(number, step, latitude, longitude)` — plus `vertical` when a pressure-level or `pv` field is selected. `number=0` is the control; `number=1..100` are perturbed members. Pressure-level fields have only the control member. `step` is daily (00Z). `tp` is a precipitation **rate** (`mm day-1`) left-labeled so `step = 0` is the first 24h (`[init, init+1d)`); known temperature fields (`t2m`, `sst`, `t`, …) are `degree_Celsius`. Stamped with `weather_skills_source=ecmwf-s2s`.

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
# Several surface fields in one call (instant `tp` and daily `t2m` are two ECDS legs)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 \
    -v tp t2m --output /tmp/ecmwf_tp_t2m.zarr
```

```bash
# 2 m temperature (the other most-used S2S field)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 -v t2m --output /tmp/ecmwf_t2m.zarr
```

```bash
# Sea-surface temperature
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 -v sst --output /tmp/ecmwf_sst.zarr
```

```bash
# Temperature on all native pressure levels (control forecast)
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 -v t --output /tmp/ecmwf_t.zarr
```

```bash
# Specific humidity + zonal wind (two ECDS legs: q is 7 levels, u is 10).
# Compose with zonal-moisture-transport for eastward IVT (`viwve`).
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 \
    -v q -v u --output /tmp/ecmwf_q_u.zarr
```

```bash
# Named country: run resolve-region first, then pass the printed N/W/S/E:
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-02-15 --bbox 5/34/-5/42 --output /tmp/ecmwf_kenya.zarr
```

See [references/REFERENCE.md](${CLAUDE_SKILL_DIR}/references/REFERENCE.md) for the exact ECDS request parameters and how retrieval time scales with area.
