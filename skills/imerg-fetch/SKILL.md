---
name: imerg-fetch
description: "Do not use as the default IMERG source. Prefer dynamical-fetch --dataset nasa-imerg-analysis-late (or nasa-imerg-analysis-early): credential-free half-hourly, supports --bbox. Use this skill only for NASA GES DISC daily Late/Final via Earthdata (GPM_3IMERGDL / GPM_3IMERGDF) when you specifically need that daily product — e.g. IMERG Final. Choose --version from the window: final once dates are past the ~3.5-month Final embargo, late for more recent dates still under that embargo."
license: MIT
compatibility: Requires Python 3.12 and uv. Authenticates to NASA Earthdata via the `earthaccess` library — set EARTHDATA_USERNAME and EARTHDATA_PASSWORD in the environment, or use a `.netrc` entry for `urs.earthdata.nasa.gov`.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  catalog-group: fetchers
  variables:
    - precip
  openclaw:
    requires:
      env:
        - EARTHDATA_USERNAME
        - EARTHDATA_PASSWORD
    primaryEnv: EARTHDATA_USERNAME
---

# imerg-fetch

Downloads IMERG daily precipitation granules from NASA GES DISC via `earthaccess` for the requested date range and writes a global-grid Zarr store. For a calendar window use `resolve-time`; for the latest published day use `--probe-latest [late|final]` — do not guess the lag. Match `--version` to that window: `final` when the dates are past the Final embargo, `late` for recent dates still inside it.

## When to use

**Default IMERG source is `dynamical-fetch`**, not this skill. Use
`--dataset nasa-imerg-analysis-late` (or `nasa-imerg-analysis-early`):
credential-free, `--bbox`, half-hourly. Aggregate to daily with
`aggregate-temporal` if you need a day grid.

Use **this** skill only when you need the NASA GES DISC **daily** Late or
Final files (`GPM_3IMERGDL` / `GPM_3IMERGDF`) over Earthdata — typically
IMERG Final, or a daily Late granule that the catalog does not replace.
Daily GES DISC can have interior holes the catalog does not; it also has
no `--bbox` (full 0.1° globe).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time YYYY-MM-DD --end-time YYYY-MM-DD --output <path.zarr> [--version late|final]
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [late|final]
```

### Arguments
- `--start-time`, `--end-time` — inclusive date range. Each value is an absolute ISO date `YYYY-MM-DD`. Calendar windows: `resolve-time last-2w`. Latest published day: `--probe-latest [late|final]`.
- `--probe-latest [late|final]` — print the latest available `YYYY-MM-DD` on stdout and exit. No `-o`. IDENT selects the release (default `late`).
- `--output`, `-o` — output Zarr path (overwritten if it exists).
- `--version` — which IMERG Daily product to fetch: `late` (default; `GPM_3IMERGDL`, ~4 days behind realtime) or `final` (`GPM_3IMERGDF`, research-quality, ~3.5 months after the observation month). Choose from the requested window, not habit — see below.

### Choosing late vs final

IMERG Daily publishes two products on different schedules. Pick `--version` from the time range:

- **final** — gauge-adjusted research product. Typical latency is ~3.5 months (the Final embargo). Use it for historical windows that Final already covers.
- **late** — near-realtime product (~4 days behind). Use it for recent dates still under the Final embargo, including verification of the last weeks or months.

This skill fetches one product per call and does not fall back from Final to Late. If a window crosses the embargo (some days Final, some only Late), either use `late` for the whole window or split into two fetches. Do not request `final` for dates Final has not published yet. `--probe-latest final` and `--probe-latest late` report the latest day each product has on disk.

### Production lag and partial-tail behavior

IMERG late runs ~4 days behind realtime, so a window whose `--end-time` is at or near
the present can include trailing days not yet published. After the fetch, the
present days are read from the written dataset's own time axis, and a span with
fewer present days than requested is classified as follows:

- A contiguous **trailing** gap (missing days are exactly the tail past the last
  present day) prints a stderr `WARNING` naming the missing days and effective
  end date; the run exits 0 with a partial dataset.
- An **interior** hole (a missing day that precedes a later present day) is a
  server/data gap rather than realtime lag, so the run exits non-zero.

If no granule day falls inside the requested window at all, the run exits
non-zero.

### Output

Zarr with data variable `precip` (mm/day) and dims `(time, latitude, longitude)` on the global IMERG 0.1° grid. Stamped with `weather_skills_source=imerg`.

### Memory and performance

There is no `--bbox` flag: the full 0.1° global grid (~3600×1800 cells, ~26 MB/day as float32) is always fetched. The skill builds the full window in memory before writing.

For tight-memory hosts, keep the window short and run the `clip-region` skill immediately after to shrink to your area of interest.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. For a fetcher this is a
length-1 array; downstream zarr-writing skills append their own entry. `args`
records `{start, end, version}`. `version` is the value printed by
`--help`. Inspect a written output's provenance with the `provenance` skill.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start-time 2026-05-01 --end-time 2026-05-10 --output /tmp/imerg.zarr
```
