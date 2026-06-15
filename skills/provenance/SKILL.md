---
name: provenance
description: Inspect the weather_skills_history provenance chain stamped on a weather-skills artifact (an envelope Zarr or a plot PNG) and render it as a human-readable lineage, the raw JSON chain, or a runnable reproduction script. Use when you need to answer "how did this file come to exist, and how do I regenerate it?" — especially for a PNG, whose chain lives in binary tEXt chunks an editor can't open.
license: MIT
compatibility: Requires Python 3.10+ and uv. Inspects a zarr directory or a .png file; reads no credentials and writes nothing.
metadata:
  version: "0.1.7"
  catalog-group: agent-tooling
---

# provenance

Read-only inspector for the `weather_skills_history` provenance chain that every
zarr-writing skill stamps on its output (and that plot-writers embed in PNG
`tEXt` chunks). It does not produce an artifact — every view prints to
stdout, and the user redirects when they want a file.

## When to use

- You have a zarr or PNG and want to know which skills produced it, in what
  order, with what arguments.
- You need to regenerate an artifact and want a starting-point reproduction
  script rather than reconstructing the pipeline by hand.
- The chain is unreadable directly: a zarr keeps it in store attrs, and a PNG
  keeps it in binary `tEXt` chunks.

Read-only: it takes no `--output` and prints the result to stdout; it never
writes a file or modifies its input.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/provenance.py --input <artifact> [--format human|json|script] [--check]
```

### Arguments
- `--input`, `-i` — the artifact to inspect: a weather-skills envelope Zarr (a
  directory) or a plot PNG (a file ending `.png`). Required.
- `--format` — output view, one of `human` (default), `json`, or `script`.
- `--check` — validate the `weather_skills_history` schema instead of rendering it.
  Takes precedence over `--format`.

A path that is neither a zarr directory nor a `.png` file, or a missing
path, is an error: a message goes to stderr and the skill exits 2. An
artifact with no `weather_skills_history` reports `no provenance recorded` and
exits 0.

In the render formats (`human`/`json`/`script`), an artifact whose
`weather_skills_history` is present but not a JSON array is treated as no history: the
skill prints a one-line malformed-history warning to stderr, reports
`no provenance recorded`, and exits 0.

## Formats

### `human`

Prints the lineage oldest-first. Each step shows its skill, version, input
basename, and args. For a two-input PNG (`plot-compare` or
`plot-mediogram`), each input branch is printed under its own label. For a
`concat` zarr, the concat step lists each input branch's full recorded lineage
beneath it (labeled `a`, `b`, … by input order). If the zarr carries a
`weather_skills_source` attr it is printed first.

### `json`

Emits the raw chain(s) to stdout as JSON. A single-branch artifact emits the
chain array; a multi-branch PNG emits a `{label: chain}` object so each
branch stays identified.

### `script`

Emits a runnable bash reproduction that regenerates the artifact. Every line
is a full literal `uvx --from git+… forecasting-skills <skill> …` command, so
nothing needs to be installed first — `uvx` fetches the CLI on demand.

- A single chain (a zarr, or a single-input PNG) reproduces linearly: each
  step's output threads into the next step's `--input`; fetch steps take no
  `--input`; intermediates write to `stepN.zarr` and the final step writes the
  artifact's own name.
- A two-input plot (`plot-compare`, `plot-mediogram`) or any multi-branch PNG
  reproduces each input branch to a distinctly-named file, then emits one final
  plot command that takes every branch's output as an input (e.g.
  `plot-compare --input a.zarr --input b.zarr`).
- A `concat` zarr records each input's full chain under the concat entry, so it
  reproduces every input branch (labeled `a`, `b`, … by input order) to its own
  `{letter}.zarr`, then emits one final `concat` command that threads every
  branch's output via repeated `--input` plus the concat's `--dim`/`--coords`
  args. A branch whose head is not a fetcher still emits the `<UPSTREAM>` caveat
  so you can supply that input yourself.

## `--check` (schema validation)

`--check` validates the `weather_skills_history` on an artifact against the array
contract in `ENVELOPE.md` and reports every violation it finds. It validates
**schema shape**, not skill-name membership: a `skill` value may be any
non-empty string, because external tools that emit `weather_skills_history` have their
own skill names.

For a zarr it reads the single `weather_skills_history` attribute; for a PNG it reads
the `weather_skills_history` key and every `weather_skills_history_<label>` tEXt key, validating
each. Each value must be a JSON array, and each entry an object with:

- `skill` — a non-empty string.
- `version` — a string.
- `args` — an object.
- `input` — `null`, a `{basename, hash}` object, or an array of
  `{basename, hash}` objects (each of which may also carry a nested `history`
  chain, which is validated recursively, so a `concat` entry's per-input
  branches are checked too).

Unknown or extra keys are noted but do not fail validation. Every violation is
reported with its location (which key, which entry index, which rule).

Exit codes:

- `0` — valid `weather_skills_history` is present.
- `1` — no `weather_skills_history` is present on the artifact.
- `2` — `weather_skills_history` is present but invalid; the violations are listed.

```bash
# Validate that a freshly produced artifact conforms to the schema.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/provenance.py -i /tmp/forecast.zarr --check
```

## Reproduction-script caveats

- **Argument translation.** Stored `args` are argparse dest names
  (underscored, e.g. `time_dim`); the script translates them back to CLI
  flags (`--time-dim`). Booleans become a bare flag when true and are
  omitted when false; list values become one repeated flag per element;
  `None` values are omitted.
- **Fetch-step credentials.** Fetch steps read credentials from the
  environment at run time. The emitted script calls the fetcher but embeds
  no secret value.

## Example

```bash
# Human-readable lineage of a clipped forecast zarr.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/provenance.py -i /tmp/ecmwf_kenya.zarr

# Raw chain as JSON.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/provenance.py -i /tmp/forecast.png --format json

# Reproduction script, saved by the user via redirect.
uv run --script ${CLAUDE_SKILL_DIR}/scripts/provenance.py -i /tmp/forecast.png --format script > repro.sh
```
