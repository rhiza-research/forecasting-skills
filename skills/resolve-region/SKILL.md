---
name: resolve-region
description: Resolve an ISO 3166-1 alpha-3 country code or a sub-national region (state, province, county) to a lat/lon bbox and optionally a boundary polygon GeoJSON. Use when you need to turn a country or county into a `--bbox N/W/S/E` value (or a polygon mask) for clip-region, ecmwf-fetch, plot, or plot-compare.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py *)
metadata:
  catalog-group: agent-tooling
---

# resolve-region

Look up a bounding box and boundary polygon for a country or a sub-national
administrative unit. The script prints a `N/W/S/E` bbox suitable for the
`--bbox` flag of the other skills, and can optionally write the boundary
polygon as a GeoJSON file for use as a `--mask-geojson` / `--geojson` polygon
mask.

Countries come from a bundled Natural Earth 1:110m admin-0 dataset, keyed by
ISO 3166-1 alpha-3 (`iso3`). States, provinces, and counties come from
[geoBoundaries](https://www.geoboundaries.org) `gbOpen` (ADM1 / ADM2), fetched
on demand per country via the public API.

## When to use

- Turning a country into a `--bbox` value for `clip-region`, `ecmwf-fetch`,
  `plot`, or `plot-compare`.
- Turning a county / state / province into a bbox or a boundary polygon.
- Producing a boundary polygon (`--geojson`) to feed `plot-compare`'s
  `--mask-geojson` or `clip-region`'s `--geojson` so grid cells outside the
  region are masked.

This does not geocode free text (no cities, basins, or street addresses). The
agent maps a place name to a country ISO3 code or a hierarchical admin name,
then this script does the deterministic geometry lookup.

## Division of labor

This script does **not** parse place names. Mapping a free-text place name to
an ISO3 code or a hierarchical admin key is the agent's job — the model is
good at it and at disambiguation. The script does the deterministic lookup
only.

### Countries

The agent resolves the name to an uppercase alpha-3 code, then calls
`resolve.py <CODE>`:

- "Kenya" → `KEN`
- "the DRC" / "Democratic Republic of the Congo" → `COD` (distinct from
  "Republic of the Congo" / "Congo-Brazzaville" → `COG`)
- "Palestine" → `PSE`

Pass a country code exactly as uppercase alpha-3. The script does not accept
lowercase or alpha-2 codes and will not silently uppercase a 3-letter token —
that is to surface a wrong code early rather than resolve the wrong country.

### Sub-national (states, provinces, counties)

Clean each name (lowercase, spaces to underscores, accents stripped) and join
levels with hyphens:

- admin-1 (state / province / Kenyan county): `country-admin1`
- admin-2 (county / district / sub-county): `country-admin1-admin2` or
  `country-admin2` when the admin-2 name is unique in that country

The country segment may be the cleaned country name **or** an ISO3 prefix:

- "Nairobi County, Kenya" → `kenya-nairobi` or `KEN-nairobi` (Kenyan counties
  are geoBoundaries ADM1)
- "California, USA" → `united_states_of_america-california` or `USA-california`
- "Westlands, Nairobi" → `kenya-nairobi-westlands` or `kenya-westlands`

Country-name aliases include `united_states` → `united_states_of_america`,
`ivory_coast`, `the_gambia`, …. Hyphens inside a single admin name stay
hyphens (`kenya-elgeyo-marakwet` is ADM1 Elgeyo-Marakwet, not admin-2). The
script matches the full remainder against ADM1 `shapeName`, then ADM2
`shapeName`, then `admin1-admin2` using ADM1 as a prefix.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py <QUERY> [--geojson PATH]
```

Print just the bbox:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN
# -> 5.506/33.893568969666944/-4.67677/41.85508309264397   (N/W/S/E)

uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py kenya-nairobi
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN-nairobi
```

Also write the boundary polygon:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN --geojson /tmp/ken.json
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py kenya-nairobi --geojson /tmp/nairobi.json
```

### Arguments

- `code` (positional) — uppercase ISO3 country code (`KEN`) or a sub-national
  region (`kenya-nairobi`, `KEN-nairobi`, `kenya-nairobi-westlands`).
- `--geojson` — optional path; writes the boundary polygon as a
  single-feature GeoJSON `FeatureCollection` (in addition to printing the bbox).

### Output

- stdout: one line, `N/W/S/E` (max lat / min lon / min lat / max lon) in decimal
  degrees — the value shape consumed by `--bbox` on the other skills.
- `--geojson PATH`: a GeoJSON `FeatureCollection` with the one matching feature.
  Properties: `iso3`, `name`, `region_name`, `level` (`country` / `admin_1` /
  `admin_2`), `country`.

Unknown codes, lowercase 3-letter tokens, and alpha-2 codes exit non-zero with
an explanation on stderr. Unknown sub-national names do too (the parent country
was recognized but the admin unit was not in geoBoundaries ADM1 or ADM2).

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
BBOX=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN)

# Resolve a Kenyan county (geoBoundaries ADM1) and mask with its polygon:
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py kenya-nairobi --geojson /tmp/nairobi.json

# Resolve a country to a boundary polygon; pass the file to the plot-compare
# skill's --mask-geojson to mask grid cells outside the country:
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN --geojson /tmp/ken.json
```

## Data

**Countries.** Natural Earth 1:110m admin-0, public domain (CC0), bundled in
weather-skills-core. Each feature keeps geometry plus `iso3` and `name`. 177
countries; micro-island states are absent at 110m.

The `iso3` key is derived from Natural Earth's `ISO_A3` property, which is
correct ISO alpha-3 for the cases NE-internal codes get wrong (e.g. `PSE` for
Palestine, not NE's `PSX`). `ISO_A3` has five `-99` gaps, filled at build time
with a fixed patch: France → `FRA`, Norway → `NOR`, Kosovo → `XKX`, N. Cyprus →
`CYN`, Somaliland → `SOL`.

**Sub-national.** [geoBoundaries](https://www.geoboundaries.org) `gbOpen` ADM1
and ADM2, CC BY 4.0 (attribution). Looked up through
`https://www.geoboundaries.org/api/current/gbOpen/{ISO3}/ADM{1|2}/`, then the
simplified GeoJSON URL in that metadata. Units are matched on `shapeName`.
Kenyan counties are ADM1 (47); Kenyan sub-counties are ADM2.
