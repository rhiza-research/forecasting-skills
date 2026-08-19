# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core[geo] @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "cftime",
#   "shapely",
# ]
# ///
"""Subset by bbox, named region, or GeoJSON polygon."""

from pathlib import Path

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import bbox_subset, clip_by_geometry, polygon_from_geojson

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


@weather_skill(
    name="clip-region",
    version=_SKILL_VERSION,
    allow_precip_totals=True,
)
@weather_skill.argument(
    "-i", "--input", type=Dataset(["spatial", "point_obs"]), required=True, dest="ds"
)
@weather_skill.argument("--bbox")
@weather_skill.argument("--region")
@weather_skill.argument(
    "--geojson",
    default=None,
    help="GeoJSON polygon path (mutex with --bbox/--region).",
)
@weather_skill.argument(
    "--keep-outside",
    action="store_true",
    help="With --geojson: NaN outside instead of dropping cells/stations.",
)
def clip_region(ds, output, bbox=None, region=None, geojson=None, keep_outside=False, **kwargs):
    """Subset by bbox, named region, or GeoJSON polygon."""
    if keep_outside and geojson is None:
        raise UsageError("--keep-outside requires --geojson")
    # Decorator may fill bbox from --region; treat filled bbox and --geojson as mutex.
    if (bbox is None) == (geojson is None):
        raise UsageError("exactly one of --bbox/--region or --geojson is required")
    if geojson is not None:
        return clip_by_geometry(
            ds,
            polygon_from_geojson(geojson, flag="--geojson"),
            drop=not keep_outside,
        )
    return bbox_subset(ds, bbox)


if __name__ == "__main__":
    clip_region()
