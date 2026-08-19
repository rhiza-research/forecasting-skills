# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
# ]
# ///
"""Resolve an ISO 3166-1 alpha-3 country code to a bbox and optional boundary polygon."""

import json
import sys
from pathlib import Path

from weather_skills_core import DataError, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

def _iter_coords(coords):
    """Yield (lon, lat) pairs by walking a nested GeoJSON coordinate array.

    Geometry coordinates nest to different depths (Polygon vs MultiPolygon);
    a position is the first level where the two leading entries are numbers.
    """
    if (
        isinstance(coords, (list, tuple))
        and len(coords) >= 2
        and isinstance(coords[0], (int, float))
        and isinstance(coords[1], (int, float))
    ):
        yield coords[0], coords[1]
        return
    for item in coords:
        yield from _iter_coords(item)

def _lon_bounds(lons):
    """Compute the (W, E) longitude interval on the circle.

    Latitude is a simple min/max, but longitude wraps at +-180, so a naive
    min/max would return a full-width box for any country crossing the 180th
    meridian (Russia, Fiji). Instead, find the largest gap between consecutive
    sorted longitudes (including the wrap-around gap from the easternmost back
    to the westernmost). The interval the geometry actually occupies is the
    complement of that largest gap:

    - If the geometry covers essentially the whole circle (Antarctica), return
      full width -180/180.
    - If the largest gap is the wrap gap, there is no antimeridian crossing:
      the ordinary W = min, E = max (W < E).
    - If the largest gap is an interior gap between L[k] and L[k+1], the country
      straddles +-180: W = L[k+1], E = L[k] (so W > E), an RFC 7946 sec 5.2
      wrapped bbox.
    """
    L = sorted(set(lons))
    n = len(L)
    if n == 1:
        return L[0], L[0]

    # gaps between consecutive sorted longitudes, plus the wrap gap from the
    # last back to the first.
    max_gap = -1.0
    gap_index = -1  # index k such that the largest gap is between L[k], L[k+1]
    for i in range(n - 1):
        gap = L[i + 1] - L[i]
        if gap > max_gap:
            max_gap = gap
            gap_index = i
    wrap_gap = (L[0] + 360.0) - L[n - 1]
    # Strict comparison so a tie is resolved in favor of the WRAPPED
    # (antimeridian-crossing) interpretation, which yields the smaller-span box.
    # Only treat the geometry as non-crossing when the wrap gap strictly exceeds
    # the largest interior gap; otherwise a true crosser whose wrap gap exactly
    # equals an interior gap would be returned as a globe-spanning normal box.
    wrap_is_largest = wrap_gap > max_gap
    if wrap_is_largest:
        max_gap = wrap_gap

    covered = 360.0 - max_gap
    if covered >= 350.0:
        # Geometry rings the globe (circumpolar, e.g. Antarctica): full width.
        return -180.0, 180.0
    if wrap_is_largest:
        # No antimeridian crossing: ordinary W < E box.
        return L[0], L[n - 1]
    # Interior largest gap: country crosses +-180. W is the start of the band
    # east of the gap, E is the end of the band west of the gap, so W > E.
    return L[gap_index + 1], L[gap_index]

def _bbox_from_geometry(geometry):
    """Compute (N, W, S, E) = (max lat, W, min lat, E).

    Latitude bounds are simple min/max. Longitude bounds are computed on the
    circle so a country crossing +-180 yields a wrapped (W > E) box and a
    circumpolar geometry yields full width; see ``_lon_bounds``.
    """
    lons = []
    min_lat = float("inf")
    max_lat = float("-inf")
    for lon, lat in _iter_coords(geometry["coordinates"]):
        lons.append(lon)
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
    w, e = _lon_bounds(lons)
    return max_lat, w, min_lat, e

@weather_skill(
    name="resolve-region",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument("code", help="ISO 3166-1 alpha-3 country code (uppercase), e.g. KEN")
@weather_skill.argument(
            "--geojson",
            help="Optional path: write the country's boundary polygon as GeoJSON",
        )
def resolve_region(code, geojson, **kwargs):
    """Resolve an ISO 3166-1 alpha-3 country code to a bbox and optional boundary polygon."""
    if len(code) != 3 or not code.isalpha() or code != code.upper():
        raise UsageError(
            f"{code!r} is not an ISO 3166-1 alpha-3 (iso3) code. "
            "Pass a three-letter uppercase code (e.g. KEN), not a name or alpha-2 code."
        )

    asset = Path(__file__).resolve().parent.parent / "assets" / "countries.geojson"
    fc = json.loads(asset.read_text())

    match = None
    for feature in fc["features"]:
        if feature["properties"]["iso3"] == code:
            match = feature
            break

    if match is None:
        raise DataError(
            f"{code!r} is not a known iso3 in the bundled Natural Earth 1:110m "
            "admin-0 dataset (177 countries; country-level only). Check the code or pick "
            "a different country."
        )

    n, w, s, e = _bbox_from_geometry(match["geometry"])

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
