---
name: analog-years
description: Return historically similar analog years for a given --date (YYYY-MM-DD). Use when a task needs ENSO / El Niño analog years, analog-year composites, or "years like this one". Stub: 2026 returns 1982 1997 2006 2015 2019 2023; any other year is an error.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/analog_years.py *)
metadata:
  version: "0.0.2"
  catalog-group: agent-tooling
---

# analog-years

Look up analog years for a calendar date. Prints the years to stdout so they
can be spliced into later fetches (same season in each analog year).

This is a **stub**. Only dates in **2026** are implemented; other years exit 2.
The analog set will be computed from data in a later version.

## When to use

- The user asks for analog years, years like this one, ENSO / El Niño analogs,
  or a composite over historically similar years.
- A pipeline needs to fetch or plot the same window in several past years
  that match the current year's climate state.

Relative phrases ("this year", "today") go through `resolve-time` first;
this skill takes an absolute `--date`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/analog_years.py --date YYYY-MM-DD \
    [--emit years|json]
```

```bash
YEARS=$(uv run ${CLAUDE_SKILL_DIR}/scripts/analog_years.py --date 2026-09-01)
# YEARS is "1982 1997 2006 2015 2019 2023"
for y in $YEARS; do
    echo "$y"
done
```

### Arguments

- `--date` — absolute ISO date `YYYY-MM-DD`. The **year** of this date is
  what is looked up; month and day are ignored in the stub.
- `--emit` — `years` (default; space-separated on stdout) or `json`.

### Output

- stdout (`years`): `1982 1997 2006 2015 2019 2023`
- stdout (`json`): `{"date":"2026-09-01","year":2026,"years":[1982,1997,2006,2015,2019,2023]}`
- stderr: `year=2026`
- Unknown years (not 2026) exit 2 with an explanation.

## Implemented years

| Input year | Analog years |
|---|---|
| 2026 | 1982, 1997, 2006, 2015, 2019, 2023 |
