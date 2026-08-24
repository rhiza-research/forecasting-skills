---
name: inspect-figure
description: "Inspect a generated plot PNG — size, whether it looks blank or uniform, a coarse color preview, and the last provenance step. Use when a figure looks wrong, empty, or unexpected, to debug before regenerating. For full lineage use provenance; for the Zarr behind the figure use inspect-zarr. After plotting, run this and also look at the PNG."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads a `.png` file; writes nothing.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_figure.py *)
metadata:
  catalog-group: agent-tooling
---

# inspect-figure

Read-only dump of a plot PNG's pixels and last provenance step. Use it to
debug a figure that looks empty, washed out, or not what you asked for —
before you regenerate the whole pipeline.

It does not print the picture (look at the PNG yourself, or Read it). It
prints structured facts: size, how much of the image is near-white or
near-black, how many distinct colors a downsample has, a 16×10 hex preview,
and the last `weather_skills_history` step if the file was stamped.

`provenance` is still the skill for the full lineage. `inspect-zarr` is still
the skill for the Zarr that fed the plot.

## When to use

- A plot skill wrote a PNG and you need to check whether it is blank or
  collapsed before showing it to the user.
- The user says the figure is wrong (empty map, missing color, wrong region).
- You want the last plot skill and its args without dumping the whole chain.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_figure.py --input <in.png> [--format human|json]
```

### Arguments

- `--input`, `-i` — plot PNG to inspect. Required. Not a Zarr.
- `--format` — `human` (default) or `json`.

Takes no `--output`. Nothing is written; stdout is the result.

A missing path, a directory, or a non-`.png` file exits 2.

### Output

**human**

```
File: map.png  18432 bytes
Image: 800 × 500 RGB PNG
Fill: 4% near-white, 1% near-black  unique colors (downsampled): 842
Flags: ok
Last skill: plot 0.0.2  style='heatmap' title='Precip'
Preview:
  #f8f8f8 #e0e0e0 ...
```

`Flags: BLANK` means the downsample is almost all near-white or near-black
(typical of all-NaN data or a bbox that missed the field). `UNIFORM` means
few distinct colors (collapsed color scale or a constant field). Notes under
the flags tell you which follow-up to run (`inspect-zarr` on the input,
`provenance` for the full chain).

**json** — the same facts as `path`, `bytes`, `width`, `height`, `mode`,
`looks_blank`, `looks_uniform`, `near_white_frac`, `near_black_frac`,
`unique_colors`, `preview`, `last_step`, `notes`.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_figure.py -i /tmp/kenya_map.png
uv run ${CLAUDE_SKILL_DIR}/scripts/inspect_figure.py -i /tmp/kenya_map.png --format json
```
