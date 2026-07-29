# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
# ]
# ///
"""Spatially subset a gridded weather-skills standard dataset Zarr."""

from weather_skills_core import Types, weather_skill
from weather_skills_core.dataset import bbox_subset, detect_spatial_dims

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"


@weather_skill(
    name="clip-region",
    version=_SKILL_VERSION,
    inputs=[Types.ANY],
    outputs=[Types.ANY],
    required_args=("bbox",),
    hash_input=False,
)
def clip_region(ds, bbox):
    """Spatially subset a gridded weather-skills standard dataset Zarr."""
    lat_dim, lon_dim = detect_spatial_dims(ds)
    return bbox_subset(ds, bbox, lat_dim=lat_dim, lon_dim=lon_dim)


if __name__ == "__main__":
    clip_region()
