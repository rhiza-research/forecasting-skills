# CLI flag conventions

Skills in this repo are independent single-file scripts, but they often expose
the same conceptual parameter under their own argparse CLI. To make skills easy
to compose and easy to work on, **a flag that does the same thing on different
skills must have the same name**.

This document is the canonical mapping. When you add or change a CLI, match
these names. New concepts that aren't covered here should be added to this file
in the same PR that introduces them.

There are no shared helpers and no lint enforcing this — skills must remain
standalone single-file scripts. The convention is enforced by review.

## Canonical names

### Inputs and outputs

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Single input Zarr | `--input` / `-i` | path | Required for skills that consume one Zarr. |
| Single output | `--output` / `-o` | path | Required for skills that produce a Zarr or other artifact. |
| Multiple inputs | `--input` / `-i`, repeated | path | Repeat the flag once per input: `-i a.zarr -i b.zarr`. Order is preserved. Skills that compare or concatenate multiple Zarrs use this form. |
| Two semantically named inputs | use a domain name (e.g. `--forecast`, `--mclimate`) | path | Use named flags only when the inputs have fixed, non-interchangeable roles AND the role name carries meaning. For symmetric or arbitrary inputs (concat, plot-compare), use `--input` repeated. |

### Region and bounding box

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Named region | `--region` | string from a per-skill fixed list | Skills that ship a hard-coded region table use `--region` and validate via argparse `choices=`. |
| Explicit bbox | `--bbox` | `N/W/S/E` decimal degrees | Slash-separated four floats. When a skill accepts both `--region` and `--bbox`, `--bbox` overrides. |

### Time

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Date range | `--start` / `--end` | relative-or-absolute date token (see grammar below); both ends inclusive | Used by archive fetchers covering a span of dates. |
| Single date | `--date` | relative-or-absolute date token (see grammar below) | Used when a skill operates on one timestamp (e.g. an init date for a forecast). |

#### Relative-or-absolute date grammar

`--start`, `--end`, and `--date` accept the **same** value grammar on every
fetcher. A value is one of:

- an absolute ISO date `YYYY-MM-DD`;
- `now` or `today` — the current UTC date;
- `latest` — the newest date with available data, discovered per source
  (imerg: max available granule date; chirps: backward HTTPS day-probe; tahmo:
  max returned observation date over a bounded lookback; ecmwf: newest available
  forecast init);
- an offset `now-<int>{d|w}` or `latest-<int>{d|w}` — the base minus N (`w` = 7
  days, so `3w` = 21 days). The offset count is capped (36525 days). Future `+`
  offsets, month/year units, and anything else are rejected with a non-zero exit
  **before any network call**.

Boundary handling for `--start`/`--end`: absolute endpoints and ordinary
relative ranges are **inclusive of both ends**. The one exception is the
**duration idiom** — start is `B-<int>{d|w}` and end is exactly the same base
token `B` (both `now`, or both `latest`): the window is exactly N days, inclusive
of `B`, with the far edge shifted in by one (so `latest-3w .. latest` →
`[latest-20d, latest]` = 21 days incl. `latest`; `now-1w .. now` → 7 days).
Tokens stay literal — `latest-3w` always means `latest − 21d`; only the
`B-N .. B` shape moves the far edge. After resolution, `start <= end` or the run
exits non-zero (pre-network).

There is no shared parser: each script carries its own copy of the value
grammar and the per-source `latest` resolver (the standalone-single-file rule
below). `latest` discovery runs at most once per invocation and only when a
token references `latest`; an all-absolute or `now`-only window performs no
discovery call. The cache key / `rhiza_history` args record the **resolved
absolute dates**, never the relative token. For any invocation containing a
relative token, the script prints a stderr line before fetching with the
resolved concrete dates, the day count, and the boundary mode and its reason.

### Variables and dimensions

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Variable selector | `--variable` / `-v` | string | Restricts an operation to one data variable in a multi-variable Zarr. |
| Spatial dim-name override | `--dims` | `LAT,LON` | Comma-separated names of the latitude and longitude dims when they're not auto-detectable. |
| Time-dim override | `--time-dim` | string | Name of the time-like dim when not auto-detectable. Distinct from `--dims`, which is spatial only. |

### Reductions and rendering

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Explicit dim reduction | `--reduce` | string, repeatable | Names a non-time dim to mean-reduce before producing a 1-D output. Repeat once per dim (`--reduce number --reduce latitude --reduce longitude`). Required (rather than silently averaging) when an input still has non-time dims after `--variable` selection. |
| Figure title | `--title` | string | Optional figure title. Used by `plot`, `plot-compare`, `plot-mediogram`, and `plot-timeseries`. |
| Output view | `--format` | `human` \| `json` \| `script` | Selects how a read-only inspector renders its result. Used by `provenance`: `human` lineage, raw `json` chain, or a runnable reproduction `script`. |

### Bias correction

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Q-Q mapping reference | `--qq-reference` | path | Optional reference Zarr whose distribution the skill maps the operation's output onto. Empirical-CDF mapping per grid cell along `--time-dim`. The reference must already be on the post-operation lat/lon grid. |

### Concurrency

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Fetch concurrency | `--workers` | int (per-skill default) | Max size of a bounded thread pool for skills that fetch many independent items (e.g. per-station or per-day requests). For network-I/O-bound work only. Keep the default conservative to respect upstream API rate limits, and let callers lower it on throttling. A concurrency knob, not a data parameter: it must be excluded from the cache key / `rhiza_history` args, since it changes speed, not output. |

## Rules

- **Multi-value parameters use repeated flags, not comma-separated values.** A
  skill that takes multiple values for the same concept repeats the flag
  (`-i a.zarr -i b.zarr`, `--country Kenya --country Uganda`) rather than
  accepting `a,b,c`. Applies beyond `--input`: any new multi-value flag follows
  the same form.
- **No backwards-compat aliasing.** If a flag name changes, change every caller
  in the same PR. There are no external callers to preserve.
- **No shared helper module.** Each skill declares its own `ArgumentParser`.
  Don't introduce `_args.py` or any cross-skill import.
- **Don't reuse a canonical name for a different concept.** If you need a new
  concept, pick a new name and add it here.
