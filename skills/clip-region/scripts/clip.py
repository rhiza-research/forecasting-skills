# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine/dim-ontology-cleanup",
#   "cftime",
# ]
# ///
"""Spatially subset a gridded weather-skills standard dataset Zarr."""

from weather_skills_core import weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"

@weather_skill(
    name="clip-region",
    version=_SKILL_VERSION,
    inputs=["space"],
    outputs=["space"]
)
@weather_skill.argument("--bbox", required=True)
def clip_region(ds, bbox, **kwargs):
    """Spatially subset a gridded weather-skills standard dataset Zarr."""
    from weather_skills_core.standard_args import bbox_subset
    from weather_skills_core.standard_dataset import detect_spatial_dims

    lat_dim, lon_dim = detect_spatial_dims(ds)
    return bbox_subset(ds, bbox, lat_dim=lat_dim, lon_dim=lon_dim)

if __name__ == "__main__":
    clip_region()
