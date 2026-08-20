# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
# ]
# ///
"""Resolve a country, named region, admin unit, or Nominatim landmark to a bbox and optional polygon."""

import json
import sys
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.region import (
    bbox_from_feature,
    geocode_nominatim,
    lookup_region,
    should_geocode,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


@weather_skill(
    name="resolve-region",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument(
    "code",
    help=(
        "ISO3 country code (uppercase, e.g. KEN), named region (East Africa), "
        "sub-national region (kenya-nairobi), or leftover place name "
        "(Mount Kenya, Kenya)"
    ),
)
@weather_skill.argument(
    "--geojson",
    help="Optional path: write the boundary polygon as GeoJSON",
)
def resolve_region(code, geojson, **kwargs):
    """Resolve a country, named region, admin unit, or Nominatim landmark to a bbox and optional polygon."""
    text = code.strip()
    if not text:
        raise UsageError(
            "pass an ISO3 code (e.g. KEN), a named region (e.g. East Africa), "
            "a sub-national region (e.g. kenya-nairobi), "
            "or a landmark (e.g. 'Mount Kenya, Kenya')."
        )
    if len(text) == 3 and text.isalpha() and text != text.upper():
        raise UsageError(
            f"{code!r} is not an ISO 3166-1 alpha-3 (iso3) code. "
            "Pass a three-letter uppercase code (e.g. KEN), not a name or alpha-2 code."
        )

    try:
        match = lookup_region(code)
    except DataError:
        if not should_geocode(code):
            raise
        match = geocode_nominatim(code)
        print(f"nominatim: {match['properties']['display_name']}", file=sys.stderr)
    n, w, s, e = bbox_from_feature(match)

    # Write the polygon (guarded) BEFORE printing the bbox, so a failed write
    # never emits a valid-looking bbox to stdout that a caller might consume.
    if geojson:
        out_fc = {"type": "FeatureCollection", "features": [match]}
        try:
            Path(geojson).write_text(json.dumps(out_fc, separators=(",", ":")))
        except OSError as exc:
            raise DataError(f"could not write boundary polygon to {geojson}: {exc}") from None
        print(f"Wrote boundary polygon: {geojson}", file=sys.stderr)

    print(f"{n}/{w}/{s}/{e}")


if __name__ == "__main__":
    resolve_region()
