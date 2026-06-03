---
name: ecmwf-fetch
description: Fetch an ECMWF S2S precipitation forecast (control + perturbed ensemble) for a date and region, writing a Rhiza Envelope Zarr. Use when a task needs raw S2S forecast precipitation for downstream aggregation, clipping, downscaling, or plotting.
license: MIT
compatibility: Requires Python 3.10+ and uv. Requires the eccodes system library for cfgrib (`brew install eccodes` or `apt install libeccodes0`). Requires ECMWF_DATASTORES_URL and ECMWF_DATASTORES_KEY in the environment (or a `~/.ecmwfdatastoresrc` file). The URL is `https://ecds.ecmwf.int/api`; the key is the personal token from your ECDS account.
metadata:
  version: "0.1.1"
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
- A downstream skill needs the forecast as a Rhiza Envelope Zarr (not raw GRIB).

Not for reanalysis, climatology, or deterministic HRES — this skill is S2S only.

## Usage

```
uv run scripts/fetch.py --date YYYY-MM-DD --region <region> --output <path.zarr>
```

### Arguments
- `--date` — forecast init date. The value is one of:
  - an absolute ISO date `YYYY-MM-DD`;
  - `now` or `today` — the current UTC date;
  - `latest` — the newest available forecast init, found by probing init dates
    backward via ECDS submits;
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
  likewise surfaced as a clear error — not a raw traceback. The main fetch's
  submit and poll use the same bounded-poll and error classification as the
  `latest` probe. Use `latest` (or an explicit init date) as the intended
  relative form for this skill; `now`/offset are accepted for grammar
  consistency with the other fetchers but seldom resolve to a valid init.

  **Cost of `latest`:** resolving `latest` is the slow case. Each probe is a real
  ECDS retrieval submit (the asynchronous queue, polled until results-ready), so
  discovering the newest init may take several minutes to an hour and steps back
  one init day at a time until one succeeds. This is acceptable because it is
  opt-in — an absolute or `now`-based `--date` does no probing. A probe job that
  ECDS marks failed/rejected means that init is not yet published and the probe
  steps back; a submit/transport/auth error, or a job still not ready after a
  bounded wall-clock poll (1 hour), is surfaced and the run exits non-zero rather
  than being misreported as a missing init or looping forever. (On the poll-cap
  timeout the run aborts rather than stepping back, because stepping back from a
  stuck-but-possibly-valid job would report a misleadingly old `latest`.) The
  completed control retrieval from the winning probe is reused as the control
  leg of the fetch, so the winning init is not submitted twice. The cache key
  records the resolved absolute init date, never the relative token.
- `--region` — one of: `africa`, `kenya`, `ghana`, `senegal`, `ethiopia`, `namibia`, `botswana`, `zambia`, `madagascar`, `angola`. Matches the named regions accepted by `clip-region`. For an explicit bbox, use `--bbox N/W/S/E` instead.
- `--bbox` — optional; `N/W/S/E` decimal degrees. Overrides `--region` if both are given.
- `--output`, `-o` — output Zarr path (overwritten if it exists).

### Output

A Zarr store with data variable `tp` (total precipitation, `kg m⁻²` — numerically equivalent to mm depth over the accumulation period) and dims `(number, step, latitude, longitude)`. `number=0` is the control; `number=1..100` are perturbed members. Stamped with `rhiza_source=ecmwf-s2s`.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
is the argparse namespace minus the `--input`/`--output` path strings;
`version` is the `_RHIZA_SKILL_VERSION` constant in `scripts/fetch.py`, kept
in lockstep with `metadata.version` in this SKILL.md by the CI version-bump
workflow.

The `args` dict stores argparse dest names (underscored, e.g. `time_dim`,
`target_resolution`, `anchor_end`), not the hyphenated CLI flag names
(`--time-dim`, `--target-resolution`, `--anchor-end`). A consumer
reconstructing a `uv run scripts/<skill>.py <args>` invocation must
translate underscore → hyphen.

## Examples

```bash
uv run scripts/fetch.py --date 2026-02-15 --region africa --output /tmp/ecmwf.zarr
```

```bash
uv run scripts/fetch.py --date 2026-02-15 --bbox 7/32/-6/43 --output /tmp/ecmwf_kenya.zarr
```

```bash
# Newest available init (slow: probes init dates backward via ECDS submits)
uv run scripts/fetch.py --date latest --region kenya --output /tmp/ecmwf_latest.zarr
```

See [references/REFERENCE.md](references/REFERENCE.md) for the exact ECDS request parameters and how retrieval time scales with area.
