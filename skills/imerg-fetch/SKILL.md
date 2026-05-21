---
name: imerg-fetch
description: Fetch live IMERG satellite precipitation for a date range and write a Rhiza Envelope Zarr. Use when a task needs recent half-hourly/daily IMERG rainfall, e.g. for station vs. satellite comparison or verification.
license: MIT
compatibility: Requires Python 3.12+ and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
metadata:
  version: "0.1.0"
  openclaw:
    requires:
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# imerg-fetch

Downloads IMERG daily precipitation granules from NASA GES DISC via `earthaccess` for the requested date range and writes a global-grid Zarr store. The IMERG late release runs ~4 days behind realtime; callers typically shift the requested end date accordingly.

## When to use

- Need recent IMERG rainfall for a forecast-verification or station-comparison task.

## Usage

```
uv run scripts/fetch.py --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr> [--version late|final]
```

### Arguments
- `--start`, `--end` — inclusive date range (ISO).
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--version` — `late` (default; ~4 days behind realtime, `GPM_3IMERGDL`) or `final` (`GPM_3IMERGDF`).

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, lat, lon)` on the global IMERG 0.1° grid. Stamped with `rhiza_source=imerg`.

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

## Example

```bash
uv run scripts/fetch.py --start 2026-01-01 --end 2026-02-07 --output /tmp/imerg.zarr
```
