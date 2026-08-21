# Weather Skills

> ⚠️ **Under active development — not production ready.**
>
> These skills are an early experiment in tool composition for weather/climate
> data pipelines. Interfaces, dataset schema, and skill boundaries may change
> without notice. Fetchers hit real APIs and require credentials; middle-
> pipeline skills have only been smoke-tested on small synthetic data. Do not
> use in any automated workflow you rely on, and do not assume outputs are
> scientifically validated. Expect breakage.

Small tools an AI agent can run to fetch weather data, transform it, and make
plots. Each skill is a command-line script. They share one Zarr file format so
outputs of one skill can feed into the next — see
[`STANDARD_DATASET.md`](https://github.com/rhiza-research/weather-skills-core/blob/main/docs/weather-skill-authoring/references/STANDARD_DATASET.md).

Built as [Agent Skills](https://agentskills.io) by Rhiza Research.

## Skills

### Fetchers (ingress — source-specific)

Prefer `dynamical-fetch` when the dynamical.org catalog has the dataset
(GFS, GEFS, ECMWF IFS-ENS, AIFS, ICON-EU, analyses, IMERG). Use a
credentialed or source-specific fetcher only when it does not.

| Skill | What it does |
|---|---|
| `dynamical-fetch` | **Preferred when the catalog has it.** dynamical.org open catalog (GFS, GEFS, ECMWF IFS-ENS, AIFS, ICON-EU, MRMS, analyses, **IMERG**) via `--dataset`, credential-free → Zarr. Default IMERG source: `nasa-imerg-analysis-late` / `nasa-imerg-analysis-early`. |
| `ecmwf-fetch` | ECMWF **S2S** ensemble (cf + pf; default `tp`, also `t2m`, `sst`, ocean, pressure levels) over a `--bbox` via ECDS → Zarr. Prefer `dynamical-fetch` for medium-range IFS-ENS / AIFS. |
| `chirps-fetch` | CHIRPS live precipitation observations → Zarr |
| `imerg-fetch` | Earthdata **daily** IMERG Late/Final fallback (`GPM_3IMERGDL` / `GPM_3IMERGDF`) → Zarr. Default IMERG is `dynamical-fetch`, not this skill. |
| `tahmo-fetch` | TAHMO station observations (daily-aggregated) → Zarr |
| `kenya-forecast-fetch` | Kenya forecasts archive raw Zarr grids (`gs://kenya-forecasting-data/<date>/data/`) → standard dataset (compose with `plot` for figures) |

### Generic middle (operate on any standard dataset)
| Skill | What it does |
|---|---|
| `resolve-region` | Resolve an ISO 3166-1 alpha-3 country code or sub-national region to a `--bbox N/W/S/E` (and optional boundary polygon GeoJSON) |
| `resolve-time` | Resolve relative calendar dates ("the last two weeks", `latest`, `now-3d`) to `--start-time`/`--end-time` or `--date`. Latest published day is the fetcher's `--probe-latest`, not this skill. |
| `inspect-zarr` | Print dimension sizes, coordinate values, and a data-variable summary of a Zarr (stdout only) |
| `clip-region` | Subset a gridded Zarr to a `--bbox N/W/S/E` (use `resolve-region` for a country's bbox) |
| `aggregate-temporal` | Resample rates along `time`/`step` (mean/min/max); duration-weights CF bounds; keeps `data_interval` when uniform; stamps `aggregation_period` + `aggregation_coverage` + `cell_methods` |
| `convert-to-totals` | Terminal: rate × stamped `aggregation_period` → amount (100% coverage default; refuses overlapping Δt < period — `select` first) |
| `deaccumulate` | Convert a leftover cumulative-since-init forecast variable into per-step diffs along the `step` axis (fetchers already write rates) |
| `step-to-time` | Realize a forecast's `step` lead-time axis as wall-clock valid times (`time = init + step`) so it can be compared against time-based observations |
| `unit-convert` | Convert a variable to target `--to-units` (e.g. precip flux `kg m-2 s-1` → depth rate `mm/day` via ÷ liquid-water density) |
| `downscale` | Spatial downscaling onto a finer grid (by factor, finer resolution, or a reference grid) via `--method` (linear-interpolation or q-q empirical quantile mapping) |
| `coarsen` | Coarsen or align a grid by linear interpolation onto a target `(resolution, offset)` — geometry only, adds no information |
| `rename` | Rename a data variable to a new name |
| `concat` | Join Zarr stores along a named dim (incl. new dims with coord values) |
| `summarize-dim` | Summarize named dims with a statistic (mean/std/min/max/sum/median) — e.g. ensemble spread as the std across `number`, or a time-mean baseline |
| `difference` | Subtract one dataset from another (A − B) with inner-join alignment and broadcasting — anomalies vs a baseline, scenario-minus-historical change maps |
| `plot` | Heatmap (optionally restricted to a `--bbox` and/or masked to a `--mask-geojson` polygon) or timeseries PNG from one dataset |
| `plot-compare` | Side-by-side multi-panel comparison of two datasets (incl. station-vs-grid), optionally clipped to a `--bbox` and masked to a `--mask-geojson` polygon |
| `plot-compare-forecasts` | N-dataset comparison grid (rows = forecasts and/or gridded obs; columns = union of times); missing times are blank `n/a` cells |
| `plot-mediogram` | ECMWF-style mediogram PNG comparing a forecast ensemble against an m-climate ensemble at a single lat/lon |
| `kenya-forecast-png` | Pre-rendered KMSA / Sheerwater Kenya forecast product PNGs from the public kenya-forecasts archive (credential-free) |

### Agent capabilities
Capabilities the agent uses alongside pipelines; none of them produces a
dataset output.

| Skill | What it does |
|---|---|
| `resolve-time` | Resolve relative calendar dates to absolute `--start-time`/`--end-time` or `--date`. |
| `inspect-zarr` | Print dims, coordinate values, and data-variable summary of a Zarr (stdout; no write). |
| `provenance` | Inspect `weather_skills_history` on a Zarr or plot PNG (lineage, JSON, or reproduction script). |
| `submit-feedback` | Build a length-checked prefilled GitHub new-issue URL the user clicks to file feedback under their own account. Holds no token, makes no network call, creates no issue itself. |

## Install

These skills live at <https://github.com/rhiza-research/forecasting-skills>.
There are three ways to use them.

### As a Claude Code plugin

Install the plugin once — add the marketplace, then install the plugin:

```bash
claude plugin marketplace add rhiza-research/forecasting-skills
claude plugin install rhiza-forecasting@weather-skills
```

Then run the bundled `forecaster` agent. The `--allowedTools` flag pre-approves
the plugin's skills for the session so a multi-step pipeline runs end to end
without a prompt at each step:

```bash
claude --agent rhiza-forecasting:forecaster --allowedTools "Skill(rhiza-forecasting:*)"
```

Fetchers still need their credentials in the environment; see each skill's
`compatibility:` frontmatter for what it reads (for example an
`EXAMPLE_API_KEY`).

To make the approval permanent instead of passing the flag every time, add the
same rule to `permissions.allow` in a settings file (`.claude/settings.json` for
one project, `~/.claude/settings.json` for every project):

```json
{
  "permissions": {
    "allow": [
      "Skill(rhiza-forecasting:*)"
    ]
  }
}
```

With that rule in place, plain `claude --agent rhiza-forecasting:forecaster`
works without the flag. This lets the plugin's skills run unprompted —
including reaching the network and writing output files — so add an `ask` or
`deny` rule instead if you want to be prompted for some or all of them.

### As a CLI tool

For ad-hoc command-line use (no agent involved), install the skills as a
single `forecasting-skills` binary:

```bash
# One-shot, no install — list available skills
uvx --from git+https://github.com/rhiza-research/forecasting-skills forecasting-skills

# Run one
uvx --from git+https://github.com/rhiza-research/forecasting-skills forecasting-skills <skill> [args]
```

Or install once and invoke directly:

```bash
uv tool install git+https://github.com/rhiza-research/forecasting-skills
forecasting-skills                          # list
forecasting-skills <skill> [args]           # run one
```

Each skill's PEP 723 inline dependency block is resolved by `uv run`
on each invocation, so the runner itself contributes no Python deps to the
script's runtime environment.

### As agent skills

For use by an LLM agent (Claude Code, etc.), install the `SKILL.md` files
into your project with [skillkit](https://github.com/rohitg00/skillkit) — no
local install needed, `npx` runs the latest skillkit on demand (add `@latest`
to always pull the newest):

```bash
# List what skillkit discovers in the repo
npx skillkit install rhiza-research/forecasting-skills --list

# Install all skills to the current project
npx skillkit install rhiza-research/forecasting-skills --all --yes

# Install globally so any project can use them
npx skillkit install rhiza-research/forecasting-skills --all --yes --global

# Target a specific agent (otherwise skillkit installs for every agent it detects)
npx skillkit install rhiza-research/forecasting-skills --all --yes --agent claude-code

# Install just a subset
npx skillkit install rhiza-research/forecasting-skills --skill=ecmwf-fetch
npx skillkit install rhiza-research/forecasting-skills --skills=clip-region,plot

# Overwrite an existing install
npx skillkit install rhiza-research/forecasting-skills --all --yes --force
```

Pin in a manifest for team / reproducible use:

```bash
npx skillkit manifest init
npx skillkit manifest add rhiza-research/forecasting-skills
npx skillkit manifest install
```

See `npx skillkit install --help` for more flags.

## Composition pattern

Middle-pipeline skills are designed to chain. Example — daily forecast plus
satellite validation for one country, using the `forecasting-skills` CLI from
the Install section above:

```bash
# Over Kenya — dummy bbox 5/34/-5/42; resolve-region prints the real N/W/S/E.
forecasting-skills ecmwf-fetch \
    --date 2026-02-13 \
    --bbox 5/34/-5/42 \
    --output /tmp/ecmwf.zarr
forecasting-skills aggregate-temporal \
    --input /tmp/ecmwf.zarr \
    --period weekly \
    --method mean \
    --output /tmp/ecmwf_weekly.zarr
forecasting-skills convert-to-totals \
    --input /tmp/ecmwf_weekly.zarr \
    --output /tmp/ecmwf_weekly_totals.zarr
forecasting-skills plot \
    --input /tmp/ecmwf_weekly_totals.zarr \
    --variable tp \
    --output /tmp/weekly.png

forecasting-skills dynamical-fetch \
    --dataset nasa-imerg-analysis-late \
    --start-time 2025-12-24 \
    --end-time 2026-02-13 \
    --bbox 5/34/-5/42 \
    -v precipitation_surface \
    --output /tmp/imerg.zarr
forecasting-skills aggregate-temporal \
    --input /tmp/imerg.zarr \
    --period dekadal \
    --method mean \
    --output /tmp/imerg_dekadal.zarr
forecasting-skills convert-to-totals \
    --input /tmp/imerg_dekadal.zarr \
    --output /tmp/imerg_dekadal_totals.zarr
forecasting-skills rename \
    --input /tmp/imerg_dekadal_totals.zarr \
    --variable precipitation_surface \
    --to-name precip \
    --output /tmp/imerg_dekadal_precip.zarr

forecasting-skills tahmo-fetch \
    --country Kenya \
    --start-time 2025-12-24 \
    --end-time 2026-02-13 \
    --output /tmp/tahmo.zarr

forecasting-skills plot-compare \
    -i /tmp/tahmo.zarr \
    -i /tmp/imerg_dekadal_precip.zarr \
    --variable precip \
    --output /tmp/sat_vs_stations.png
```

In practice a user just states the goal in natural language and the agent
picks and composes skills from this set.

## Standard dataset contract

Skills share a simple Zarr contract: fixed dimension names (`lat`, `lon`, `time`,
`init_time`, …) and short types (`spatial`, `observations`, `forecast`,
`vertical_forecast`, `point_obs`, …).
See
[`STANDARD_DATASET.md`](https://github.com/rhiza-research/weather-skills-core/blob/main/docs/weather-skill-authoring/references/STANDARD_DATASET.md).
Fetchers write that shape; other skills only depend on dims, coords, data
variables, and `weather_skills_*` attrs — not on per-variable encoding.

## CLI flag conventions

Each skill declares its CLI through the `@weather_skill` decorator from
`weather_skills_core`, so common parameters (`--input` / `-o`, `--bbox`,
`--start-time` / `--end-time`, etc.) mean the same thing wherever they appear.
See
[`CONVENTIONS.md`](https://github.com/rhiza-research/weather-skills-core/blob/main/docs/weather-skill-authoring/references/CONVENTIONS.md)
for the full mapping of concept → canonical flag.

## Credentials

Fetchers read credentials from environment variables (or `.netrc` where
supported by the underlying client). Nothing is hardcoded. See each skill's
`compatibility:` frontmatter for the specific vars required.

## Alternatives considered

See [`ALTERNATIVES.md`](ALTERNATIVES.md) for the tools we surveyed (CDO, GDAL,
xcube, xclim, zarrs_tools, …) and why the current lightweight xarray-based
implementations are the right trade at this scale.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the publishing model (`main` is the
consumer-facing branch — every merge is a release), the PR workflow, and the
version-bump conventions (per-skill `_SKILL_VERSION` in scripts, driven by `release: major`
/ `release: minor` PR labels, with patch as the default).

## License

MIT. See [`LICENSE`](LICENSE).
