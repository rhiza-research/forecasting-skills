# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core",
#   "cftime",
#   "shapely",
# ]
#
# [tool.uv.sources]
# weather-skills-core = { path = "../../../../weather-skills-core", editable = true }
# ///
"""Subset by bbox or GeoJSON polygon."""

from weather_skills_core import Types, UsageError, weather_skill
from weather_skills_core.dataset import bbox_subset, clip_by_geometry, polygon_from_geojson

_SKILL_VERSION = "0.1.11"


@weather_skill(
    name="clip-region",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.ANY],
    optional_args=("bbox",),
    hash_input=False,
)
@weather_skill.argument("--geojson", default=None, help="GeoJSON polygon path (mutex with --bbox).")
@weather_skill.argument(
    "--keep-outside",
    action="store_true",
    help="With --geojson: NaN outside instead of dropping.",
)
def clip_region(ds, bbox, geojson, keep_outside):
    """Subset by bbox or GeoJSON polygon."""
    if (bbox is None) == (geojson is None):
        raise UsageError("exactly one of --bbox or --geojson is required")
    if geojson is not None:
        return clip_by_geometry(ds, polygon_from_geojson(geojson, flag="--geojson"), drop=not keep_outside)
    return bbox_subset(ds, bbox)


if __name__ == "__main__":
    clip_region()
