---
name: resolve-region
description: Resolve an ISO 3166-1 alpha-3 country code to a lat/lon bbox (and optionally a boundary polygon GeoJSON) from a bundled Natural Earth 1:110m admin-0 dataset. Use when you need to turn a country into a `--bbox N/W/S/E` value (or a polygon mask) for clip-region, ecmwf-fetch, plot, or plot-compare.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run --script ${CLAUDE_SKILL_DIR}/scripts/resolve.py *)
metadata:
  version: "0.1.6"
  catalog-group: agent-tooling
---

# resolve-region

Look up a country's bounding box and boundary polygon from a bundled,
country-level boundary dataset, keyed by ISO 3166-1 alpha-3 (`iso3`) code. The
script prints a `N/W/S/E` bbox suitable for the `--bbox` flag of the other
skills, and can optionally write the country's boundary polygon as a GeoJSON
file for use as a `--mask-geojson` polygon mask.

## When to use

- Turning a country into a `--bbox` value for `clip-region`, `ecmwf-fetch`,
  `plot`, or `plot-compare`.
- Producing a country boundary polygon (`--geojson`) to feed `plot-compare`'s
  `--mask-geojson` so grid cells outside the country are masked.

This is country-level only. It does not resolve sub-country places (provinces,
cities, basins), and it does not geocode free text.

## Division of labor

This script does **not** parse place names. Mapping a free-text place name to
its ISO 3166-1 alpha-3 code is the agent's job — the model is good at it and at
disambiguation. The script does the deterministic `iso3` → geometry lookup only.

The agent resolves the name to a code, then calls `resolve.py <CODE>`:

- "Kenya" → `KEN`
- "the DRC" / "Democratic Republic of the Congo" → `COD` (distinct from
  "Republic of the Congo" / "Congo-Brazzaville" → `COG`)
- "Palestine" → `PSE`

Pass the code exactly as uppercase alpha-3. The script does not accept lowercase
or alpha-2 codes and will not silently uppercase — that is to surface a wrong
code early rather than resolve the wrong country.

## Usage

```
uv run --script ${CLAUDE_SKILL_DIR}/scripts/resolve.py <CODE> [--geojson PATH]
```

Print just the bbox:

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN
# -> 5.506/33.893568969666944/-4.67677/41.85508309264397   (N/W/S/E)
```

Also write the boundary polygon:

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN --geojson /tmp/ken.json
```

### Arguments

- `code` (positional) — ISO 3166-1 alpha-3 country code, uppercase (e.g. `KEN`).
- `--geojson` — optional path; writes the country's boundary polygon as a
  single-feature GeoJSON `FeatureCollection` (in addition to printing the bbox).

### Output

- stdout: one line, `N/W/S/E` (max lat / min lon / min lat / max lon) in decimal
  degrees — the value shape consumed by `--bbox` on the other skills.
- `--geojson PATH`: a GeoJSON `FeatureCollection` with the one matching country
  feature (geometry + `iso3`/`name` properties).

Unknown codes, lowercase, and alpha-2 codes exit non-zero with an explanation on
stderr.

### Antimeridian (wrapped bboxes)

A country whose territory crosses the 180° meridian (e.g. Russia, Fiji) returns
a **wrapped** bbox where **west is greater than east** — an RFC 7946 §5.2
antimeridian-crossing bounding box. For example, Russia's longitude band runs
from roughly `19` eastward across 180° to roughly `-169`, so `W ≈ 19` and
`E ≈ -169` with `W > E`. The forecasting skills' `--bbox` consumers
(`clip-region`, `plot`, `plot-compare`, `ecmwf-fetch`) honor this: when `W > E`
they select the two longitude bands `lon ≥ W` and `lon ≤ E` rather than the
empty `slice(W, E)`. A genuinely circumpolar geometry (Antarctica) instead
returns the full width `-180`/`180`.

## Examples

```bash
# Resolve a country to a bbox; pass the printed value to a --bbox consumer
# such as the clip-region, ecmwf-fetch, plot, or plot-compare skill:
BBOX=$(uv run --script ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN)

# Resolve a country to a boundary polygon; pass the file to the plot-compare
# skill's --mask-geojson to mask grid cells outside the country:
uv run --script ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN --geojson /tmp/ken.json
```

## Data

Boundaries are Natural Earth 1:110m admin-0 countries, which are in the public
domain (CC0). The bundled `assets/countries.geojson` is a slimmed copy: each
feature keeps only its geometry plus `iso3` and `name` properties.

This resolution is country-level only: 177 countries, no sub-country places and
no micro-island states (they are absent at 110m).

The `iso3` key is derived from Natural Earth's `ISO_A3` property, which is
correct ISO alpha-3 for the cases NE-internal codes get wrong (e.g. `PSE` for
Palestine, not NE's `PSX`). `ISO_A3` has five `-99` gaps, filled at build time
with a fixed patch: France → `FRA`, Norway → `NOR`, Kosovo → `XKX`, N. Cyprus →
`CYN`, Somaliland → `SOL`.
