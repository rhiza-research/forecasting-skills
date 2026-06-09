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
| Explicit bbox | `--bbox` | `N/W/S/E` decimal degrees | Slash-separated four floats. The canonical way to spatially restrict a skill. To get a country's bbox, resolve it with the `resolve-region` skill. |
| Country code (resolve input) | positional `<CODE>` | ISO 3166-1 alpha-3, uppercase | `resolve-region`'s positional argument. The script does an exact `iso3` → geometry lookup; mapping a free-text place name to the code is the agent's job. |
| Boundary polygon output | `--geojson` | path | `resolve-region` output: writes the resolved country's boundary polygon as a single-feature GeoJSON `FeatureCollection`. |
| Polygon-mask input | `--mask-geojson` | path | A GeoJSON boundary polygon used to NaN-mask gridded cells outside it. Used by `plot-compare`; feed it `resolve-region`'s `--geojson` output. |

### Time

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Date range | `--start` / `--end` | relative-or-absolute date token (see grammar below); both ends inclusive | Used by archive fetchers covering a span of dates. |
| Single date | `--date` | relative-or-absolute date token (see grammar below) | Used when a skill operates on one timestamp (e.g. an init date for a forecast). |
| Target CF calendar | `--calendar` | CF calendar name | Calendar to convert the time axis onto (e.g. `standard`, `proleptic_gregorian`, `noleap`, `360_day`, `all_leap`, `julian`). Used by `convert-calendar`. |
| Calendar alignment mode | `--align-on` | `date` \| `year` | How `convert-calendar` maps dates across calendars. Required whenever the source or target calendar is `360_day`. `year` translates by relative position in the year (best for daily/sub-daily); `date` conserves month/day and drops invalid dates (best for coarser-than-daily). |

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
| Variable selector | `--variable` / `-v` | string | Restricts an operation to one data variable in a multi-variable Zarr. Repeat once per variable to select several. |
| Per-input variable selector | `--variable-a` / `--variable-b` | string | Selects the variable for each input independently when a skill compares two inputs that may hold different variables. Used by `plot-compare`. Precedence per row: `--variable-a`/`-b`, then `--variable`, then that input's first real data var. |
| Catalog dataset selector | `--dataset` | string | Names which dataset to fetch from a multi-dataset source catalog. Validated at runtime against the source's own listing (e.g. `dynamical-fetch` checks `dynamical_catalog.list()`); an unknown id prints the valid list and exits. Used by fetchers that front a catalog of datasets rather than a single product. |
| Spatial dim-name override | `--dims` | `LAT,LON` | Comma-separated names of the latitude and longitude dims when they're not auto-detectable. |
| Time-dim override | `--time-dim` | string | Name of the time-like dim when not auto-detectable. Distinct from `--dims`, which is spatial only. |
| Overpass selector | `--overpass` | string from a per-skill fixed list | Which satellite half-orbit overpass to read when a product splits ascending/descending passes (e.g. `smap-fetch` uses `choices={AM, PM}`: AM = ~6am descending, PM = ~6pm ascending). |

### Catalog facet selectors

Some sources are catalogs faceted along several axes rather than a single
dataset id. A fetcher fronting such a catalog (e.g. `cmip6-fetch`) resolves a
single store from these facet flags. Reuse these names; add new ones here.

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Model / source | `--model` | string | The source model id (CMIP6 `source_id`). |
| Experiment / scenario | `--experiment` | string | The experiment or scenario id (CMIP6 `experiment_id`, e.g. `historical`, `ssp245`). |
| Ensemble member | `--member` | string | The ensemble member / variant id (CMIP6 `member_id`, e.g. `r1i1p1f1`). |
| Catalog table / frequency | `--table` | string | The catalog table id that fixes the variable's frequency/realm (CMIP6 `table_id`, e.g. `Amon`, `day`). |
| Grid label | `--grid` | string | The catalog grid label (CMIP6 `grid_label`, e.g. `gn`, `gr1`). Required only when more than one matches the other facets. |

When the catalog stores one variable per dataset (CMIP6), `--variable`/`-v`
selects that variable facet (and is the output variable).

### Reductions and rendering

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Explicit dim reduction | `--reduce` | string, repeatable | Names a non-time dim to mean-reduce before producing a 1-D output. Repeat once per dim (`--reduce number --reduce latitude --reduce longitude`). Required (rather than silently averaging) when an input still has non-time dims after `--variable` selection. |
| Figure title | `--title` | string | Optional figure title. Used by `plot`, `plot-compare`, `plot-mediogram`, and `plot-timeseries`. |
| Colormap | `--colormap` | string | matplotlib colormap name. Used by `plot-compare` (when omitted in shared-scale mode, the categorical precipitation colormap with `BoundaryNorm` is used). |
| Per-input colormap | `--colormap-a` / `--colormap-b` | string | matplotlib colormap for each input independently in `plot-compare`'s independent-scale mode. Precedence per row: `--colormap-a`/`-b`, then `--colormap`, then `viridis`. |
| Color-scale mode | `--shared-scale` / `--independent-scale` | flag (mutually exclusive) | Forces one shared color scale across both rows, or a per-row scale + colorbar. Used by `plot-compare`. Default: shared when both rows resolve to the same variable AND matching units, else independent. |
| Output view | `--format` | `human` \| `json` \| `script` | Selects how a read-only inspector renders its result. Used by `provenance`: `human` lineage, raw `json` chain, or a runnable reproduction `script`. |

### Spatial grid targets

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Refinement/coarsening factor | `--factor` / `-f` | int | Integer grid factor. For `downscale` (finer-or-equal): factor must be an integer `>= 1`; new spacing = input spacing / factor (factor 1 = identity). |
| Target grid spacing | `--target-resolution` | float (degrees) | Target grid spacing in degrees. For `downscale` it must be finer-or-equal (`<=`) to the input on each axis; for `coarsen` it must be coarser-or-equal (`>=`) on each axis. Equal resolution is a valid no-op in both. |
| Grid offset | `--offset` | float (degrees) | Used by `coarsen`: target points fall at `offset + k*resolution`. |
| Reference-grid target | `--reference-grid` | path | Path to a Zarr whose lat/lon grid defines the target grid. Used by `downscale` to match another dataset's (finer-or-equal) grid. |
| Downscaling method | `--method` | string from a per-skill fixed list | How a downscaling step adds information. `downscale` uses `choices={linear-interpolation, q-q}`. (Note: `aggregate-temporal` also uses `--method` for its temporal reducer — a per-skill fixed list, distinct values.) |

### Bias correction

| Concept | Flag | Value shape | Notes |
| --- | --- | --- | --- |
| Q-Q mapping reference | `--qq-reference` | path | Reference Zarr whose distribution the skill maps the operation's output onto. Empirical-CDF mapping per grid cell along `--time-dim`. The reference must already be on the post-operation lat/lon grid. Used by `downscale`'s `q-q` method (required there, along with `--time-dim`). |

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
