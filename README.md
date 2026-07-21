# Weather Skills

> ⚠️ **Under active development — not production ready.**
>
> These skills are an early experiment in tool composition for weather/climate
> data pipelines. Interfaces, envelope schema, and skill boundaries may change
> without notice. Fetchers hit real APIs and require credentials; middle-
> pipeline skills have only been smoke-tested on small synthetic data. Do not
> use in any automated workflow you rely on, and do not assume outputs are
> scientifically validated. Expect breakage.

A set of composable [Agent Skills](https://agentskills.io) for building
weather/climate data pipelines from an LLM-driven agent. Skills are
source-specific fetchers (ingress), generic operators that work on a shared
Zarr-based container (see [`ENVELOPE.md`](ENVELOPE.md)), or capabilities the
agent uses alongside pipelines.

Initiated by Rhiza Research.

## Skills

### Fetchers (ingress — source-specific)
| Skill | What it does |
|---|---|
| `ecmwf-fetch` | ECMWF S2S ensemble precipitation forecast (cf + pf) over a `--bbox` (use `resolve-region` for a country's bbox) via ECDS → Zarr |
| `chirps-fetch` | CHIRPS live precipitation observations → Zarr |
| `imerg-fetch` | IMERG satellite precipitation (late release) → Zarr |
| `tahmo-fetch` | TAHMO station observations (daily-aggregated) → Zarr |
| `dynamical-fetch` | dynamical.org open catalog (GFS, GEFS, ECMWF IFS-ENS, AIFS, ICON-EU, MRMS, analyses) via `--dataset`, credential-free → Zarr |

### Generic middle (operate on any envelope)
| Skill | What it does |
|---|---|
| `resolve-region` | Resolve an ISO 3166-1 alpha-3 country code to a `--bbox N/W/S/E` (and optional boundary polygon GeoJSON) from bundled Natural Earth 1:110m boundaries |
| `clip-region` | Subset a gridded Zarr to a `--bbox N/W/S/E` (use `resolve-region` for a country's bbox) |
| `aggregate-temporal` | Resample along `time` or `step` into daily/weekly/dekadal/monthly windows |
| `deaccumulate` | Convert a cumulative-since-init forecast variable (e.g. ECMWF S2S `tp`) into per-step diffs along the `step` axis |
| `step-to-time` | Realize a forecast's `step` lead-time axis as wall-clock valid times (`time = init + step`) so it can be compared against time-based observations |
| `unit-convert` | Convert a variable to target `--to-units` (e.g. precip flux `kg m-2 s-1` → depth rate `mm/day`, via a liquid-water density bridge) |
| `downscale` | Spatial downscaling onto a finer grid (by factor, finer resolution, or a reference grid) via `--method` (linear-interpolation or q-q empirical quantile mapping) |
| `coarsen` | Coarsen or align a grid by linear interpolation onto a target `(resolution, offset)` — geometry only, adds no information |
| `rename` | Rename a data variable to a new name |
| `concat` | Join Zarr stores along a named dim (incl. new dims with coord values) |
| `reduce` | Collapse named dims with a statistic (mean/std/min/max/sum/median) — e.g. ensemble spread as the std across `number`, or a time-mean baseline |
| `difference` | Subtract one envelope from another (A − B) with inner-join alignment and broadcasting — anomalies vs a baseline, scenario-minus-historical change maps |
| `plot` | Heatmap (optionally restricted to a `--bbox` and/or masked to a `--mask-geojson` polygon) or timeseries PNG from one dataset |
| `plot-compare` | Side-by-side multi-panel comparison of two datasets (incl. station-vs-grid), optionally clipped to a `--bbox` and masked to a `--mask-geojson` polygon |
| `plot-mediogram` | ECMWF-style mediogram PNG comparing a forecast ensemble against an m-climate ensemble at a single lat/lon |

### Agent capabilities
Capabilities the agent uses alongside pipelines; none of them produces an
envelope output.

| Skill | What it does |
|---|---|
| `email-report` | Compose an RFC 5322 `.eml` with attachments. **Mocks SMTP — writes to disk, does not send.** |
| `submit-feedback` | Build a length-checked prefilled GitHub new-issue URL the user clicks to file feedback under their own account. Holds no token, makes no network call, creates no issue itself. |

## Install

These skills live at <https://github.com/rhiza-research/forecasting-skills>.
There are two ways to use them.

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

Each skill's PEP 723 inline dependency block is resolved by `uv run --script`
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
npx skillkit install rhiza-research/forecasting-skills --skills=clip-region,plot,email-report

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
# Resolve the country bbox once, reuse it across the fetch and the clip.
KENYA_BBOX=$(forecasting-skills resolve-region KEN)

forecasting-skills ecmwf-fetch \
    --date 2026-02-13 \
    --bbox "$KENYA_BBOX" \
    --output /tmp/ecmwf.zarr
forecasting-skills aggregate-temporal \
    --input /tmp/ecmwf.zarr \
    --period weekly \
    --method sum \
    --output /tmp/ecmwf_weekly.zarr
forecasting-skills plot \
    --input /tmp/ecmwf_weekly.zarr \
    --variable tp \
    --output /tmp/weekly.png

forecasting-skills imerg-fetch \
    --start 2025-12-24 \
    --end 2026-02-13 \
    --output /tmp/imerg.zarr
forecasting-skills clip-region \
    --input /tmp/imerg.zarr \
    --bbox "$KENYA_BBOX" \
    --output /tmp/imerg_kenya.zarr
forecasting-skills aggregate-temporal \
    --input /tmp/imerg_kenya.zarr \
    --period dekadal \
    --method sum \
    --output /tmp/imerg_dekadal.zarr

forecasting-skills tahmo-fetch \
    --country Kenya \
    --start 2025-12-24 \
    --end 2026-02-13 \
    --output /tmp/tahmo.zarr

forecasting-skills plot-compare \
    -i /tmp/tahmo.zarr \
    -i /tmp/imerg_dekadal.zarr \
    --variable precip \
    --output /tmp/sat_vs_stations.png

forecasting-skills email-report \
    --from "Sender <sender@example.com>" \
    --to "recipient@example.com" \
    --subject "Daily Outlook" \
    --body-file body.txt \
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
`weather_skills_*` attrs, never on per-variable codec encoding.

## CLI flag conventions

Each skill ships its own argparse CLI, but they share canonical flag names so
common parameters (`--input` / `-o`, `--bbox`, `--start` / `--end`,
etc.) mean the same thing wherever they appear. See
[`CONVENTIONS.md`](CONVENTIONS.md) for the full mapping of concept → canonical
flag.

## Credentials

Fetchers read credentials from environment variables (or `.netrc` where
supported by the underlying client). Nothing is hardcoded. See each skill's
`compatibility:` frontmatter for the specific vars required.

## What `allowed-tools` pre-approves

Every skill declares one pre-approved command in its `SKILL.md` frontmatter:

```
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/<script>.py *)
```

The pattern fixes the leading program and the script path: a matching command
has to begin with `uv run --script` followed by that one script inside the
skill's own directory. Everything after that is a single trailing `*`, which
matches the whole argument tail, shell metacharacters included. Claude Code's
[permission rules](https://code.claude.com/docs/en/permissions) split a command
on the separators `&&`, `||`, `;`, `|`, `|&`, `&`, and newlines and require each
subcommand to match a rule on its own, so the grant does not carry over to a
chained second command. Beyond that split the argument tail is not parsed
against the pattern: constructs that stay inside a single command, such as
command substitution with `$(...)` and output redirection with `>`, are part of
what the `*` matches. The same documentation warns that "Bash permission
patterns that try to constrain command arguments are fragile." Read this grant
as pinning down the program and the script, and not as a constraint on what the
arguments can do.

An agent running under the grant therefore invokes that script with any flags it
chooses and no per-call prompt. The following happen without confirmation:

- **Network requests to data providers.** Every fetcher reaches its source over
  the network, and the `--bbox`, `--start`, and `--end` values decide how much
  is transferred. Each fetcher and where it connects:

  | Skill | Reaches |
  |---|---|
  | `arco-era5-fetch` | `gs://gcp-public-data-arco-era5/…` on Google Cloud Storage, read anonymously through `gcsfs` |
  | `chirps-fetch` | `data.chc.ucsb.edu` |
  | `cmip6-fetch` | the catalog at `storage.googleapis.com/cmip6/pangeo-cmip6.csv`, then the `gs://` store named by the matched catalog row, read anonymously through `gcsfs` |
  | `dynamical-fetch` | the dynamical.org open catalog on AWS Open Data, through the `dynamical-catalog` library |
  | `ecmwf-fetch` | the ECMWF data store at the URL in `ECMWF_DATASTORES_URL` (`https://ecds.ecmwf.int/api`) |
  | `ghcn-daily-fetch` | `noaa-ghcn-pds.s3.amazonaws.com` |
  | `imerg-fetch` | NASA Earthdata through `earthaccess`: authentication against `urs.earthdata.nasa.gov`, then the granule hosts the CMR search returns |
  | `oisst-fetch` | the NOAA PSL OPeNDAP server at `psl.noaa.gov/thredds/…` |
  | `openaq-fetch` | `api.openaq.org/v3` |
  | `smap-fetch` | NASA Earthdata through `earthaccess`, the same path as `imerg-fetch` |
  | `tahmo-fetch` | the TAHMO API, through the TAHMO Python SDK |

  No other skill opens a network connection of its own; `resolve-region` reads
  boundaries bundled in the skill directory.
- **Use of credentials already in the environment.** Fetchers that need
  authentication read it themselves: `ECMWF_DATASTORES_URL` /
  `ECMWF_DATASTORES_KEY`, `TAHMO_API_USERNAME` / `TAHMO_API_PASSWORD`,
  `OPENAQ_API_KEY`, and NASA Earthdata credentials via environment variables or
  a `.netrc` entry. Whatever is set when the agent runs is what gets used.
- **Dependency resolution at run time.** Each script's PEP 723 inline dependency
  block is resolved by `uv run --script` on invocation, against the
  `<script>.py.lock` file committed alongside it, which downloads and installs
  packages into uv's cache on first use. `tahmo-fetch` additionally installs
  from a git repository — `https://github.com/rhiza-research/tahmo-api`, pinned
  in its script metadata to commit `8ed3adc`.
- **Writes to any path passed as an output argument.** `--output` is not
  confined to a working directory, and missing parent directories are created.
  What happens to something already sitting at that path depends on what the
  skill writes:
  - `plot`, `plot-compare`, `plot-mediogram`, `plot-timeseries` and
    `email-report` write one file and overwrite whatever is there.
    `resolve-region`'s optional `--geojson` path behaves the same way.
  - The Zarr-writing skills replace a whole store: the directory at `--output`
    is deleted and written fresh, so its previous contents are gone. Against a
    regular file rather than a directory they diverge. `arco-era5-fetch`,
    `cmip6-fetch` and `oisst-fetch` delete the file and write the store in its
    place. `rename` and `select` reject the path and exit 2. The remaining
    Zarr writers call `shutil.rmtree` on it, which raises `NotADirectoryError`
    and leaves the file untouched.

This is the trade the grant makes: an agent composes a whole pipeline without
interrupting the user at every step, and in exchange the user pre-authorizes
that behavior for every script in the set. Run these skills with credentials
scoped to what the task needs, and point `--output` at a directory you are
willing to have written to.

To require a prompt for a skill, add an `ask` rule (or a `deny` rule to block it
outright) to a Claude Code settings file — `.claude/settings.json` for one
project, `~/.claude/settings.json` for every project. Permission rules are
evaluated deny, then ask, then allow, so a matching `ask` rule prompts even
though the skill's `allow` grant matches the same command:

```json
{
  "permissions": {
    "ask": [
      "Bash(uv run --script */chirps-fetch/scripts/fetch.py *)"
    ]
  }
}
```

A settings rule is where this belongs: the `SKILL.md` files live inside the
managed plugin install and are rewritten by the next plugin update.

## Alternatives considered

See [`ALTERNATIVES.md`](ALTERNATIVES.md) for the tools we surveyed (CDO, GDAL,
xcube, xclim, zarrs_tools, …) and why the current lightweight xarray-based
implementations are the right trade at this scale.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the publishing model (`main` is the
consumer-facing branch — every merge is a release), the PR workflow, and the
version-bump conventions (per-skill `metadata.version` driven by `release: major`
/ `release: minor` PR labels, with patch as the default).

## License

MIT. See [`LICENSE`](LICENSE).
