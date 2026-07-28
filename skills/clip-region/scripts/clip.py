# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
# ]
# ///
"""Spatially subset a gridded weather-skills envelope Zarr."""

from weather_skills_core import WroteSummary, types, validate_type, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.11"


@weather_skill(
    "clip-region",
    _SKILL_VERSION,
    input_type=types.ALL,
    output_type=types.ALL,
    bbox=types.REQUIRED,
    dims=True,
    hash_input=False,
    cache_hit_label="clip",
)
def clip_region(ds, bbox, dims):
    """Spatially subset a gridded weather-skills envelope Zarr."""
    from weather_skills_core.envelope import bbox_subset, detect_spatial_dims

    lat_dim, lon_dim = detect_spatial_dims(ds, dims)
    sub = bbox_subset(ds, bbox, lat_dim=lat_dim, lon_dim=lon_dim)
    # Subsetting the spatial axes preserves the envelope shape.
    validate_type(sub, ds)
    return sub, WroteSummary(f"{sub.sizes}", replace=True)


if __name__ == "__main__":
    clip_region()
