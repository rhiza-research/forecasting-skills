---
name: resolve-time
description: Resolve a relative date query (the last two weeks, today, latest, last month, now-3d) to absolute `--start-time`/`--end-time` or `--date` values, using the current UTC date and the publication lag / embargo of a named fetcher. Use before any fetch when the user said a relative window rather than YYYY-MM-DD.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py *)
metadata:
  catalog-group: agent-tooling
---

# resolve-time

Turn a relative time expression into the absolute `YYYY-MM-DD` values that
fetchers actually accept. The script prints `--start-time`/`--end-time` (or
`--date` for a forecast init) suitable for splicing into the next skill, and
applies the named product's embargo or publication lag so the window is one
the fetcher can fill.

Fetchers do **not** parse `latest`, `now-3d`, or "last two weeks". Always run
this first, then pass the printed flags through.

## When to use

- The user asked for "the last two weeks", "today", "latest", "yesterday",
  "this month", or any other relative / rolling window.
- You are about to call a fetcher and need to know how far from realtime that
  product actually is (CHIRPS pentad lag, ECMWF S2S 2-day embargo, IMERG late
  ~4 days, ERA5 ~5 days, …).
- You need today's date as a real calendar value, not a guess.

## Division of labor

This script does **not** parse free-text English. Mapping a phrase to a query
token is the agent's job — the model is good at that. The script does the
deterministic calendar math, the UTC clock, and the product embargo.

Map the phrase, then call `resolve.py <TOKEN> --product <skill>` (or
`skill:dataset` when the fetcher has more than one time shape, e.g.
`dynamical-fetch:noaa-gfs-forecast`):

| Phrase | Token |
|---|---|
| the last two weeks / last 14 days | `last-2w` (same window as `last-14d`) |
| last 7 days / the past week (rolling) | `last-7d` |
| today / now / latest available | `latest` |
| yesterday | `yesterday` |
| 3 days ago | `now-3d` |
| this week (ISO, Mon–Sun, so far) | `this-week` |
| last week (previous complete ISO week) | `last-week` |
| this month so far / last month / this year | `this-month` / `last-month` / `this-year` |
| March 2026 / calendar 2024 | `2026-03` / `2024` |
| an already-absolute day or span | `2026-08-01` or `2026-08-01/2026-08-14` |

Pass `--product` whenever the next skill is a known fetcher. Rolling tokens
(`latest`, `last-2w`) end on that product's latest available date, not on
today — so "the last two weeks of CHIRPS" is two weeks of CHIRPS data, not
two calendar weeks with a missing tail.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py <QUERY> [--product PRODUCT] \
    [--as-of YYYY-MM-DD] [--emit flags|iso|json]
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py --list-products
```

```bash
# "the last two weeks" of CHIRPS — stdout is flags for chirps-fetch:
TIME=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-2w --product chirps-fetch \
    --as-of 2026-08-20)
# TIME is --start-time 2026-08-02 --end-time 2026-08-15

# Latest GFS forecast init from dynamical.org (not bare dynamical-fetch):
INIT=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py latest \
    --product dynamical-fetch:noaa-gfs-forecast --as-of 2026-08-20)
# INIT is --date 2026-08-19
```

### Arguments

- `query` (positional) — token from the table above. Not English.
- `--product` — catalog key: the fetcher skill (`chirps-fetch`, `ecmwf-fetch`)
  or `skill:dataset` when one skill serves several clocks
  (`dynamical-fetch:noaa-gfs-forecast`, `imerg-fetch:final`). Optional: omit
  only when no fetcher follows (then `latest` is today's UTC date and there
  is no lag). `--list-products` prints the catalog. Bare `dynamical-fetch`
  is an error that lists the dataset keys.
- `--as-of` — clock date `YYYY-MM-DD`. Default: today's date in UTC. Pass it
  to reproduce a window or to honor an explicit "as of Friday".
- `--emit` — `flags` (default; splice onto the next skill), `iso`
  (`YYYY-MM-DD` or `START/END`), or `json`.
- `--list-products` — print the embargo catalog and exit.

### Output

- stdout (`flags`): `--start-time YYYY-MM-DD --end-time YYYY-MM-DD` for range
  fetchers, or `--date YYYY-MM-DD` for a forecast init (`ecmwf-fetch`,
  `kenya-forecast-fetch`). Capture this and splice it into the fetcher.
- stderr: one line with `as_of`, `product`, lag note, and `available_through`
  (plus `clipped …` when an absolute range was trimmed to coverage).
- Inclusive dates: `last-1d` is one day; `last-2w` is 14 days ending on the
  latest available date.

A range token (`last-2w`, `this-month`, `YYYY-MM`) against a `--date` product
(`ecmwf-fetch`) exits non-zero — those fetchers take one init, so use `latest`
or a `YYYY-MM-DD`. A point token (`latest`, `yesterday`, `now-3d`) against a
range fetcher prints a one-day `--start-time`/`--end-time`.

Unknown tokens, English phrases, and dates inside an embargo (e.g. `yesterday`
on ECMWF S2S) exit 2 with an explanation. Rolling tokens never fail that way:
they snap to `available_through`.

## Calendar rules

- Clock is **UTC**. `--as-of` overrides it.
- Weeks are **ISO** (Monday start). `this-week` is Monday through `as_of`
  (clipped by the product). `last-week` is the previous complete ISO week.
- `last-<N>d|w|m|y` is a rolling inclusive window ending on the product's
  latest available date (or `as_of` when there is no product). Months/years
  clamp the day (31 Jan − 1 month → 28/29 Feb).
- `this-month` / `this-year` are "so far". `YYYY-MM` / `YYYY` are the full
  calendar period, then clipped to product coverage.
- `now-<N>d|w|m|y` is a single date offset from `as_of` (not from the
  embargoed clock). If that date is still inside the embargo, the run fails
  rather than silently snapping — use `latest` when you want the most recent
  allowed date.

## Product lags

These are conservative "available through" values so the next fetch does not
open on a missing tail. They are not a live inventory of the remote store.

Each fetcher declares the lag on `metadata.availability` in its SKILL.md
(core owns the calendar math). This skill reads those files from the sibling
skills directory at runtime — run `--list-products` for the catalog. Do not
copy the table here; it would drift.

`--list-products` prints skill name, shape, and lag. An absolute range that
overruns `available_through` is clipped (stderr says so). A range that starts
after coverage ends, or before coverage begins and collapses, exits 2.

## Examples

```bash
# Last two weeks of CHIRPS (pentad-aware); splice stdout onto chirps-fetch:
TIME=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-2w --product chirps-fetch)

# Latest GFS forecast init; splice stdout onto dynamical-fetch --dataset:
INIT=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py latest \
    --product dynamical-fetch:noaa-gfs-forecast)

# Pin the clock (tests / "as of 1 August"):
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-7d --product imerg-fetch \
    --as-of 2026-08-01 --emit iso
```
