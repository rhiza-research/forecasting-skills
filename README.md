# Rhiza Skills

A set of composable [Agent Skills](https://agentskills.io) for building
weather/climate data pipelines from an LLM-driven agent. Skills are either
source-specific I/O (fetchers, email egress) or generic operators that work on
a shared Zarr-based container (see [`ENVELOPE.md`](ENVELOPE.md)).

## Skills

### Fetchers (ingress — source-specific)
| Skill | What it does |
|---|---|
| `ecmwf-fetch` | ECMWF S2S ensemble precipitation forecast (cf + pf) via MARS → Zarr |
| `chirps-fetch` | CHIRPS live precipitation observations → Zarr |
| `imerg-fetch` | IMERG satellite precipitation (late release) → Zarr |
| `tahmo-fetch` | TAHMO station observations (daily-aggregated) → Zarr |

### Generic middle (operate on any envelope)
| Skill | What it does |
|---|---|
| `clip-region` | Subset a gridded Zarr to a named region or `--bbox N/W/S/E` |
| `aggregate-temporal` | Resample along `time` or `step` into daily/weekly/dekadal/monthly windows |
| `downscale` | Spatial coarsening by factor or target resolution; multiple reducers |
| `concat` | Join Zarr stores along a named dim (incl. new dims with coord values) |
| `plot` | Heatmap or timeseries PNG from one dataset |
| `plot-compare` | Side-by-side multi-panel comparison of two datasets (incl. station-vs-grid) |

### Egress
| Skill | What it does |
|---|---|
| `email-report` | Compose an RFC 5322 `.eml` with attachments. **Mocks SMTP — writes to disk, does not send.** |

## Install

These skills live at <https://github.com/rhiza-research/skills>. Install them
with [skillkit](https://github.com/rohitg00/skillkit) — no local install
needed, `npx` runs the latest skillkit on demand (add `@latest` to always
pull the newest):

```bash
# List what skillkit discovers in the repo
npx skillkit install rhiza-research/skills --list

# Install all 11 skills to the current project
npx skillkit install rhiza-research/skills --all --yes

# Install globally so any project can use them
npx skillkit install rhiza-research/skills --all --yes --global

# Target a specific agent (otherwise skillkit installs for every agent it detects)
npx skillkit install rhiza-research/skills --all --yes --agent claude-code

# Install just a subset
npx skillkit install rhiza-research/skills --skill=ecmwf-fetch
npx skillkit install rhiza-research/skills --skills=clip-region,plot,email-report

# Overwrite an existing install
npx skillkit install rhiza-research/skills --all --yes --force
```

Pin in a manifest for team / reproducible use:

```bash
npx skillkit manifest init
npx skillkit manifest add rhiza-research/skills
npx skillkit manifest install
```

See `npx skillkit install --help` for more flags.

## Composition pattern

Middle-pipeline skills are designed to chain. Example — daily forecast plus
satellite validation for one country:

```bash
uv run ecmwf-fetch/scripts/fetch.py \
    --date 2026-02-13 --region kenya --output /tmp/ecmwf.zarr
uv run aggregate-temporal/scripts/aggregate.py \
    --input /tmp/ecmwf.zarr --period weekly --method sum \
    --output /tmp/ecmwf_weekly.zarr
uv run plot/scripts/plot.py \
    --input /tmp/ecmwf_weekly.zarr --variable tp \
    --output /tmp/weekly.png

uv run imerg-fetch/scripts/fetch.py \
    --start 2025-12-24 --end 2026-02-13 --output /tmp/imerg.zarr
uv run clip-region/scripts/clip.py \
    --input /tmp/imerg.zarr --region kenya --output /tmp/imerg_kenya.zarr
uv run aggregate-temporal/scripts/aggregate.py \
    --input /tmp/imerg_kenya.zarr --period dekadal --method sum \
    --output /tmp/imerg_dekadal.zarr

uv run tahmo-fetch/scripts/fetch.py \
    --country Kenya --start 2025-12-24 --end 2026-02-13 \
    --output /tmp/tahmo.zarr

uv run plot-compare/scripts/plot_compare.py \
    --a /tmp/tahmo.zarr --b /tmp/imerg_dekadal.zarr --variable precip \
    --output /tmp/sat_vs_stations.png

uv run email-report/scripts/email.py \
    --from "Sender <sender@example.com>" \
    --to "recipient@example.com" \
    --subject "Daily Outlook" --body-file body.txt \
    --attach /tmp/weekly.png /tmp/sat_vs_stations.png \
    --output /tmp/kenya.eml
```

In practice a user just states the goal in natural language and the agent
picks and composes skills from this set.

## Envelope contract

The generic middle skills rely on a shared Zarr shape — gridded
`(number?, step|time, latitude, longitude)` or station
`(time, station_id)` — documented in [`ENVELOPE.md`](ENVELOPE.md). Fetchers
produce an envelope; consumers only rely on dims, coords, data variables and
`rhiza_*` attrs, never on per-variable codec encoding.

## Credentials

Fetchers read credentials from environment variables (or `.netrc` where
supported by the underlying client). Nothing is hardcoded. See each skill's
`compatibility:` frontmatter for the specific vars required.

## Alternatives considered

See [`ALTERNATIVES.md`](ALTERNATIVES.md) for the tools we surveyed (CDO, GDAL,
xcube, xclim, zarrs_tools, …) and why the current lightweight xarray-based
implementations are the right trade at this scale.

## License

TBD — add before publishing.
