---
name: ecmwf-fetch
description: Fetch an ECMWF S2S precipitation forecast (control + perturbed ensemble) for a date and bbox from the ECMWF Data Stores (ECDS), writing a weather-skills envelope Zarr. Use when a task needs raw S2S forecast precipitation for downstream aggregation, clipping, downscaling, or plotting. To fetch over a country, get its bbox from the resolve-region skill first.
license: MIT
compatibility: Requires Python 3.12 and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). Requires ECMWF_DATASTORES_URL and ECMWF_DATASTORES_KEY in the environment (or a `~/.ecmwfdatastoresrc` file). The URL is `https://ecds.ecmwf.int/api`; the key is the personal token from your ECDS account.
metadata:
  version: "0.1.10"
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
- A downstream skill needs the forecast as a weather-skills envelope Zarr (not raw GRIB).

Not for reanalysis, climatology, or deterministic HRES — this skill is S2S only.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --bbox N/W/S/E --output <path.zarr>
```

### Arguments
- `--date` — forecast init date. The value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — the newest *accessible* forecast init, found by probing init dates
    backward via ECDS submits. Recent ECMWF S2S real-time data is
    access-restricted (embargoed) for a window of variable width, so `latest`
    skips embargoed inits and resolves to the newest init you can actually
    retrieve;
  - an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
    days, so `3w` = 21 days). The offset is capped at 36525 days; a larger value,
    a future `+` offset, a month/year unit, or any malformed value exits 2 before
    any network call.

  For a relative token the resolved concrete init date is echoed to stderr before
  fetching, e.g. `resolved "latest" -> 2026-05-30 (single forecast init date)`.

  **`now`/offset values rarely land on a real init day.** ECMWF S2S runs init on
  fixed days (Mondays and Thursdays), so an arbitrary calendar date — which is
  what `now`, `today`, and `now-<int>{d|w}` resolve to — usually is not a
  published init. When the requested init is not retrievable (ECDS rejects the
  job) the main fetch exits non-zero with a clear "no data for this init (it may
  not be a valid S2S init day)" message, and a transport/auth failure is
  likewise surfaced as a clear error — not a raw traceback. If the requested
  init is inside the S2S real-time embargo (access-restricted), the error says
  so explicitly and points at `latest` or an older init date. Prefer `latest`
  (or an explicit init date); `now`/offset values are accepted but seldom
  resolve to a valid init.

  **Cost of `latest`:** resolving `latest` is the slow case. Each probe is a real
  ECDS retrieval submit (the asynchronous queue, polled until results-ready), so
  one init day at a time until one succeeds. An absolute or `now`-based `--date`
  does no probing. A job ECDS marks failed/rejected because that init is not yet
  published steps back. A probe failure matching the S2S real-time embargo
  signature ("Restricted access to S2S" in the error text) also steps back,
  since `latest` resolves to the newest accessible init. That signature is the
  dividing line: a credential, transport, or HTTP failure does not match it, so
  the run exits non-zero with the original error instead of misreporting it as a
  missing init. A probe job still not results-ready after a bounded wall-clock
  poll (1 hour) is treated as stuck and also exits non-zero rather than stepping
  back, because stepping back from a stuck-but-possibly-valid job would report a
  misleadingly old `latest`. The whole discovery loop is additionally bounded by
  a one-hour time budget. If every probed init in the lookback window was
  access-restricted, the run exits non-zero with a message to check S2S access
  and license terms. The cache key records the resolved absolute init date,
  never the relative token.
- `--bbox` — required; `N/W/S/E` decimal degrees. The retrieval area (smaller bbox = faster retrieval). To fetch over a country, get its bbox from the `resolve-region` skill and pass the value here.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A Zarr store with data variable `tp` (total precipitation, `kg m⁻²` — numerically equivalent to mm depth over the accumulation period) and dims `(number, step, latitude, longitude)`. `number=0` is the control; `number=1..100` are perturbed members. Stamped with `weather_skills_source=ecmwf-s2s`.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
records the run's flag values under underscored names (e.g. a flag
`--time-dim` is recorded as `time_dim`); `version` is the value printed by
`--help`. Inspect a written output's provenance with the `provenance` skill.

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
