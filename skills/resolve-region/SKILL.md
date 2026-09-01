---
name: resolve-region
description: Resolve an ISO 3166-1 alpha-3 country code, a Natural Earth multi-country region (East Africa, Western Africa), a custom forecast box (Kenya OND region), a sub-national region (state, province, county), or a leftover landmark name to a lat/lon bbox and optionally a boundary polygon GeoJSON. Use when you need to turn a country, county, or named place into a `--bbox N/W/S/E` value (or a polygon mask) for clip-region, ecmwf-fetch, plot, or plot-compare. Prefer ISO3 / country-admin1 / Natural Earth region names / custom forecast boxes; Nominatim is the fallback for landmarks.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py *)
metadata:
  version: "0.0.2"
  catalog-group: agent-tooling
---

# resolve-region

Look up a bounding box and boundary polygon for a country, a named
multi-country region, a custom forecast box, a sub-national administrative
unit, or a leftover place name (landmark, city that is not an admin key). The
script prints a `N/W/S/E` bbox suitable for the `--bbox` flag of the other
skills, and can optionally write the boundary polygon as a GeoJSON file for
use as a `--mask-geojson` / `--geojson` polygon mask.

Countries come from a bundled Natural Earth 1:110m admin-0 dataset, keyed by
ISO 3166-1 alpha-3 (`iso3`). Multi-country names dissolve those countries by
Natural Earth's continent / UN subregion / World Bank region labels. States,
provinces, and counties come from
[geoBoundaries](https://www.geoboundaries.org) `gbOpen` (ADM1 / ADM2), fetched
on demand per country via the public API. A few briefing boxes that are not
Natural Earth labels (e.g. `Kenya OND region`) are bundled as rectangles.
Queries that are not an ISO3 code, Natural Earth region, custom box, or admin
key fall through to
[OSM Nominatim](https://nominatim.org) (`limit=1`).

## When to use

- Turning a country into a `--bbox` value for `clip-region`, `ecmwf-fetch`,
  `plot`, or `plot-compare`.
- Turning a Natural Earth multi-country region (East Africa, Western Africa)
  or a custom forecast box (Kenya OND region) into a bbox without hitting
  Nominatim.
- Turning a county / state / province into a bbox or a boundary polygon.
- Turning a landmark (Mount Kenya, a lake, a city that is not an admin unit)
  into a bbox when you do not have an ISO3 / `country-admin1` / named-region key.
- Producing a boundary polygon (`--geojson`) to feed `plot-compare`'s
  `--mask-geojson` or `clip-region`'s `--geojson` so grid cells outside the
  region are masked.

Prefer ISO3, named regions, and hierarchical admin keys when you have them —
those lookups are offline and do not hit Nominatim.

## Division of labor

The agent maps a country to uppercase ISO3, a multi-country domain to a named
region (`East Africa`), a briefing box to a custom forecast name
(`Kenya OND region`), and a county to a hierarchical admin key. This
script does that deterministic geometry lookup first. Leftover free text that
is **not** an ISO3-shaped token, **not** a named region or custom box, and
**not** a `country-admin…` key is sent to Nominatim (one search, first hit).
Misspelled admin keys (`kenya-nairbi`) error instead of guessing via OSM.
Disambiguate landmarks by passing a more specific string
(`Mount Kenya, Kenya`), not extra flags. Do **not** send "East Africa" or
"Kenya OND region" to Nominatim — OSM's first hit is a POI, not the
briefing domain.

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

### Named regions (Natural Earth groupings)

Pass the region as written (`East Africa`, `Western Africa`,
`Sub-Saharan Africa`). The script dissolves the bundled Natural Earth 1:110m
countries by `SUBREGION` / `CONTINENT` / `REGION_UN` / `REGION_WB` **before**
Nominatim. OSM's first hit for "East Africa" is a POI, not the geographic
region.

Directional short forms map onto Natural Earth's labels when they do not
collide with a primary name: `East Africa` → `Eastern Africa`,
`West Africa` → `Western Africa`, `Central Africa` → `Middle Africa`.
Country names still win (`South Africa` is ZAF, not Southern Africa).

`--geojson` writes the member-country MultiPolygon. `level` is `region`.
This is the Natural Earth / UN-style Eastern Africa (includes Madagascar,
Mozambique, Zambia), not a custom forecast box.

### Custom forecast boxes

A few briefing regions that are not Natural Earth groupings or admin units
are bundled as rectangles (`level` is `custom`). They never hit Nominatim.

- `Kenya OND region` (also `Kenya OND`, `OND Kenya`,
  `Central-Eastern Kenya`, `CE Kenya`) — short-rains analog box
  `1.0/36.5/-3.0/39.0` (N/W/S/E), inside Kenya, east of the Rift.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py "Kenya OND region"
# -> 1.0/36.5/-3.0/39.0
```

`--geojson` writes that rectangle (not a county union).

### Landmarks (Nominatim fallback)

Pass the place as written (`Mount Kenya`, `Lake Victoria, Kenya`). Do not
invent an admin key. The script searches
`https://nominatim.openstreetmap.org/search` once (`format=jsonv2`, `limit=1`,
`polygon_geojson=1`) and uses that hit's `boundingbox` (`south, north, west,
east` → printed `N/W/S/E`). stderr starts with `nominatim: {display_name}` so
you can see what OSM picked.

Public Nominatim is donated capacity
([usage policy](https://operations.osmfoundation.org/policies/nominatim/)): one
request per run, identifying User-Agent, no bulk loops or autocomplete. Use it
for a single user-triggered place, not a list of POIs.

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
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py "East Africa"
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py "Kenya OND region"
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py "Mount Kenya, Kenya"
```

Also write the boundary polygon:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN --geojson /tmp/ken.json
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py kenya-nairobi --geojson /tmp/nairobi.json
```

### Arguments

- `code` (positional) — uppercase ISO3 (`KEN`), a named region (`East Africa`),
  a custom forecast box (`Kenya OND region`), a sub-national region
  (`kenya-nairobi`, `KEN-nairobi`, `kenya-nairobi-westlands`), or a leftover
  landmark (`Mount Kenya, Kenya`).
- `--geojson` — optional path; writes the boundary polygon as a
  single-feature GeoJSON `FeatureCollection` (in addition to printing the bbox).
  Nominatim polygons are used when OSM returns Polygon/MultiPolygon; otherwise
  a rectangle from the Nominatim bounding box.

### Output

- stdout: one line, `N/W/S/E` (max lat / min lon / min lat / max lon) in decimal
  degrees — the value shape consumed by `--bbox` on the other skills.
- `--geojson PATH`: a GeoJSON `FeatureCollection` with the one matching feature.
  Properties: `iso3`, `name`, `region_name`, `level` (`country` / `region` /
  `custom` / `admin_1` / `admin_2` / `nominatim`), `country`. Named regions,
  custom boxes, and Nominatim hits also have `bbox` (`N, W, S, E`); `iso3` /
  `country` may be null except on custom boxes (those keep the parent country).

Unknown codes, lowercase 3-letter tokens, and alpha-2 codes exit non-zero with
an explanation on stderr. Unknown sub-national names do too (the parent country
was recognized but the admin unit was not in geoBoundaries ADM1 or ADM2) — those
do **not** fall through to Nominatim. A landmark with no Nominatim hit also
exits non-zero.

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

# Named multi-country region (Natural Earth Eastern Africa; not Nominatim):
BBOX=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py "East Africa")

# Custom forecast box (Kenya OND region; not Nominatim):
BBOX=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py "Kenya OND region")

# Landmark bbox (Nominatim). stderr shows the OSM display_name:
BBOX=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py "Mount Kenya, Kenya")

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

**Named regions.** Natural Earth continent / UN subregion / World Bank region
labels, joined to the bundled countries (offline). `East Africa` is the
`Eastern Africa` subregion, not a Nominatim POI.

**Custom forecast boxes.** Rectangles listed in weather-skills-core
`region.py` (offline). `Kenya OND region` is `1.0/36.5/-3.0/39.0`, not
a geoBoundaries county union and not Nominatim.

**Sub-national.** [geoBoundaries](https://www.geoboundaries.org) `gbOpen` ADM1
and ADM2, CC BY 4.0 (attribution). Looked up through
`https://www.geoboundaries.org/api/current/gbOpen/{ISO3}/ADM{1|2}/`, then the
simplified GeoJSON URL in that metadata. Units are matched on `shapeName`.
Kenyan counties are ADM1 (47); Kenyan sub-counties are ADM2.

**Landmarks.** [OpenStreetMap](https://www.openstreetmap.org/copyright)
Nominatim search, ODbL 1.0 (share-alike / attribution). One request per skill
run to `https://nominatim.openstreetmap.org/search`. Do not scrape lists of
places through this skill.
