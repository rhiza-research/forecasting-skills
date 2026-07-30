---
name: chirps-fetch
description: Fetch CHIRPS precipitation observations for a date range — the validated final product back to 1998, with a preliminary fallback for very recent days — and write a weather-skills standard dataset. Use when a task needs CHIRPS rainfall, recent or historical, e.g. to compare against a forecast or station data, or to build a reference period.
license: MIT
compatibility: Requires Python 3.12 and uv. Fetches over HTTPS from the public CHIRPS data server (data.chc.ucsb.edu); no credentials required.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.1.18"
  catalog-group: fetchers
---

# chirps-fetch

Downloads CHIRPS v3.0 daily `sat` precipitation for the requested date range and writes a global-grid Zarr store. Each day is taken from the validated **final** product (a per-year archive covering 1998 to present) when available, falling back to the **preliminary** product for very recent days the final has not finalized yet. When both exist for a day, final is used.

## When to use

- A task needs CHIRPS rainfall as gridded observations — recent days, a historical period, or a reference/normal year (final coverage runs 1998 to present).
- A downstream skill will clip, aggregate, or compare CHIRPS against other sources.

Coverage starts in 1998 (CHIRPS v3.0 `sat`); dates before 1998 are unavailable and exit non-zero.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/fetch.py --start YYYY-MM-DD --end YYYY-MM-DD --output <path.zarr>
```

### Arguments
- `--start`, `--end` — inclusive date range. Each value is `YYYY-MM-DD` or `latest` (newest available CHIRPS prelim day (HTTPS probe)). Both ends inclusive. See CONVENTIONS date grammar.
