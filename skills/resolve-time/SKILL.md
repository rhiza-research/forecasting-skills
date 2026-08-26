---
name: resolve-time
description: Resolve a relative date query (the last two weeks, today, latest, last 30 days, now-3d) to absolute `--start-time`/`--end-time` or `--date` values from the current UTC date. Use before any fetch when the user said a relative window rather than YYYY-MM-DD. Does not know product lag — for the latest published day, run that fetcher with `--probe-latest`.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py *)
metadata:
  catalog-group: agent-tooling
---

# resolve-time

Turn a relative time expression into the absolute `YYYY-MM-DD` values that
fetchers actually accept. The script prints `--start-time`/`--end-time` (or
`--date` for a single day) suitable for splicing into the next skill.

This is **calendar math only**. It does not probe a fetcher or clip to what
is on disk. Fetchers do **not** parse `latest`, `now-3d`, or "last two weeks"
— run this first (or `--probe-latest` when you need the latest published
day), then pass the printed flags through.

## When to use

- The user asked for "the last two weeks", "today", "yesterday", "this month",
  or any other relative / rolling window against the calendar.
- You need today's date as a real calendar value, not a guess.

For the latest day a **product** has published, do not use this skill's
`latest` token. Run that fetcher with `--probe-latest` (stdout is
`YYYY-MM-DD` or `none`). To end a rolling window on that day, pass it as
`--as-of`.

## Division of labor

This script does **not** parse free-text English. Mapping a phrase to a query
token is the agent's job — the model is good at that. The script does the
deterministic calendar math and the UTC clock.

| Phrase | Token |
|---|---|
| the last two weeks / last 14 days | `last-2w` (same window as `last-14d`) |
| last 7 days / the past week (rolling) | `last-7d` |
| the last month / last 30 days / the past month (rolling) | `last-30d` |
| today / now / latest calendar day | `latest` |
| yesterday | `yesterday` |
| 3 days ago | `now-3d` |
| this week (ISO, Mon–Sun, so far) | `this-week` |
| last week (previous complete ISO week) | `last-week` |
| this month so far | `this-month` |
| previous calendar month | `last-month` |
| this year so far / previous calendar year | `this-year` / `last-year` |
| March 2026 / calendar 2024 | `2026-03` / `2024` |
| an already-absolute day or span | `2026-08-01` or `2026-08-01/2026-08-14` |

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py <QUERY> [--as-of YYYY-MM-DD] \
    [--emit flags|iso|json]
```

```bash
# "the last two weeks" on the calendar (UTC today):
TIME=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-2w)

# Last two weeks ending on the latest published CHIRPS day:
END=$(uv run skills/chirps-fetch/scripts/fetch.py --probe-latest)
TIME=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-2w --as-of "$END")

# Latest GFS init — probe the fetcher, do not use resolve-time latest:
INIT=$(uv run skills/dynamical-fetch/scripts/fetch.py --probe-latest noaa-gfs-forecast)
```

### Arguments

- `query` (positional) — token from the table above. Not English.
- `--as-of` — clock date `YYYY-MM-DD`. Default: today's date in UTC. Pass it
  to reproduce a window, honor an explicit "as of Friday", or end a rolling
  window on a probed latest day.
- `--emit` — `flags` (default; splice onto the next skill), `iso`
  (`YYYY-MM-DD` or `START/END`), or `json`.

### Output

- stdout (`flags`): `--start-time YYYY-MM-DD --end-time YYYY-MM-DD` for a
  range, or `--date YYYY-MM-DD` for a single day (`latest`, `yesterday`,
  `now-3d`, `YYYY-MM-DD`). Capture this and splice it into the fetcher.
  Range fetchers that need a one-day window take `--start-time`/`--end-time`
  both set to the probed date — not this skill's `--date` form.
- stderr: `as_of=YYYY-MM-DD`.
- Inclusive dates: `last-1d` is one day; `last-2w` is 14 days ending on
  `as_of`.

Unknown tokens and English phrases exit 2 with an explanation.

## Calendar rules

- Clock is **UTC**. `--as-of` overrides it.
- Weeks are **ISO** (Monday start). `this-week` is Monday through `as_of`.
  `last-week` is the previous complete ISO week.
- `last-<N>d|w|m|y` is a rolling inclusive window ending on `as_of`.
  Months/years clamp the day (31 Jan − 1 month → 28/29 Feb).
  "The last month of data" is `last-30d`, not `last-month`.
- `last-week` / `last-month` / `last-year` are the previous complete
  calendar period (ISO week, named month, calendar year).
- `this-month` / `this-year` are "so far". `YYYY-MM` / `YYYY` are the full
  calendar period (not clipped to today).
- `now-<N>d|w|m|y` is a single date offset from `as_of`.

## Examples

```bash
# Last two calendar weeks, then fetch:
TIME=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-2w)
uv run skills/chirps-fetch/scripts/fetch.py $TIME -o chirps.zarr

# Pin the clock (tests / "as of 1 August"):
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-7d --as-of 2026-08-01 --emit iso
```
