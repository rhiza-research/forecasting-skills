# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime",
#   "shapely",
# ]
# ///
"""Subset by bbox or GeoJSON polygon."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import bbox_subset, clip_by_geometry, polygon_from_geojson

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(
    name="clip-region",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset(["spatial", "point_obs"]), required=True)
@weather_skill.argument("--bbox")
@weather_skill.argument(
    "--geojson",
    default=None,
    help="GeoJSON polygon path (mutex with --bbox).",
)
@weather_skill.argument(
    "--keep-outside",
    action="store_true",
    help="With --geojson: NaN outside instead of dropping cells/stations.",
)
def clip_region(ds, output, bbox=None, geojson=None, keep_outside=False, **kwargs):
    """Subset by bbox or GeoJSON polygon."""
    if keep_outside and geojson is None:
        raise UsageError("--keep-outside requires --geojson")
    if (bbox is None) == (geojson is None):
        raise UsageError("exactly one of --bbox or --geojson is required")
    if geojson is not None:
        return clip_by_geometry(
            ds,
            polygon_from_geojson(geojson, flag="--geojson"),
            drop=not keep_outside,
        )
    return bbox_subset(ds, bbox)


if __name__ == "__main__":
    clip_region()
