---
name: imerg-fetch
description: Fetch live IMERG satellite precipitation for a date range and write a Rhiza Envelope Zarr. Use when a task needs recent half-hourly/daily IMERG rainfall, e.g. for station vs. satellite comparison or verification.
license: MIT
compatibility: Requires Python 3.12+ and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
metadata:
  version: "0.1.1"
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

Pick one of two window modes:

```
# Absolute window
uv run scripts/fetch.py --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr> [--version late|final]

# Relative window (auto-discovered end)
uv run scripts/fetch.py --last <N>d|w [--anchor today|YYYY-MM-DD] --output <path.zarr> [--version late|final]
```

`--last` is mutually exclusive with `--start`/`--end`: supply one mode or the
other. Passing both, or neither, exits 2 with a clear message before any
network call.

### Arguments
- `--start`, `--end` — inclusive date range (ISO).
- `--last` — relative window length as `<int>d` (days) or `<int>w` (weeks, where
  `1w` = 7 days), e.g. `21d` or `3w`. The window is `N` inclusive calendar days.
  The end is auto-discovered as the latest available IMERG granule on or before
  `--anchor`, so the caller never guesses the production lag; the start is `end -
  (N-1)` days. Before the download, the resolved concrete window is echoed to
  stderr as `resolved --last <spec> (anchor=<date>) -> <start>..<end> (<N> days)`.
  If fewer than `N` distinct days are actually available in the resolved span (a
  data gap or near the dataset start), a stderr `WARNING` names the covered count
  and the available days are written rather than erroring or silently presenting
  a short series as complete.
- `--anchor` — upper bound for `--last` end-granule discovery: `today` (default)
  or an ISO date `YYYY-MM-DD`. Only valid with `--last`; passing it alongside
  `--start`/`--end` exits 2 with a clear message before any network call.
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--version` — `late` (default; ~4 days behind realtime, `GPM_3IMERGDL`) or `final` (`GPM_3IMERGDF`).

For `--last`, the provenance/cache `args` records the resolved concrete
`{start, end, version}`, never the `--last`/`--anchor` inputs, so the same
resolved window cache-hits and a relative spec never false-hits across days.

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, lat, lon)` on the global IMERG 0.1° grid. Stamped with `rhiza_source=imerg`.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
records the resolved concrete window as `{start, end, version}` — for `--last`,
the auto-discovered `start`/`end`, not the relative spec — so the cache key is a
function of the data produced, not how the window was requested. `version` is
the `_RHIZA_SKILL_VERSION` constant in `scripts/fetch.py`, kept in lockstep with
`metadata.version` in this SKILL.md by the CI version-bump workflow.

## Example

```bash
# Absolute window
uv run scripts/fetch.py --start 2026-01-01 --end 2026-02-07 --output /tmp/imerg.zarr

# Last 3 weeks ending at the latest granule on or before today
uv run scripts/fetch.py --last 3w --output /tmp/imerg.zarr

# Last 7 days ending at the latest granule on or before a fixed anchor
uv run scripts/fetch.py --last 7d --anchor 2026-05-01 --output /tmp/imerg.zarr
```
