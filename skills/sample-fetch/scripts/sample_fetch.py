# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "xarray",
#   "zarr>=3",
#   "cftime",
# ]
# ///
"""Return a bundled sample of a weather-skills data source, named by its provenance source string, without contacting a data provider."""

from pathlib import Path

import xarray as xr
from weather_skills_core import UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# Resolved from the script's own location so the skill works from any cwd and
# inside the shipped plugin payload.
_ASSETS_DIR = (Path(__file__).resolve().parent.parent / "assets").resolve()


def _bundled_samples():
    """Source string to asset path for every sample under `assets/`.

    Only the leading path segment is a provider prefix, so just the first
    separator becomes a colon. One mapping serves both directions, so a source
    string can never name a path the listing would not report.
    """
    return {
        path.relative_to(_ASSETS_DIR).as_posix().removesuffix(".zarr").replace("/", ":", 1): path
        for path in sorted(_ASSETS_DIR.rglob("*.zarr"))
        if path.is_dir()
    }


@weather_skill(
    name="sample-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "--source",
    required=True,
    help=(
        "Data source whose bundled sample to return, spelled exactly as that "
        "sample's weather_skills_source attr. An unrecognized value lists every "
        "available source."
    ),
)
def sample_fetch(source, **kwargs):
    """Return a bundled sample of a weather-skills data source, named by its provenance source string, without contacting a data provider."""
    samples = _bundled_samples()
    if not samples:
        raise UsageError(
            f"no bundled samples found under {_ASSETS_DIR}; this skill's assets "
            "directory is missing or empty, so the install is incomplete."
        )
    if source not in samples:
        raise UsageError(
            f"no bundled sample for --source {source!r}. Available sources: {', '.join(samples)}."
        )
    return xr.open_zarr(samples[source], consolidated=True)


if __name__ == "__main__":
    sample_fetch()
