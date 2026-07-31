# WeatherSkills standard dataset

The skills in this repo consume and produce a shared Zarr-based container: a
CF-compliant store with gridded and point_obs shapes, `weather_skills_*`
attributes, and the append-only `weather_skills_history` provenance chain that
every standard-dataset Zarr and figure PNG carries.

The enforced definition of the standard dataset, its attributes, and the
`weather_skills_history` schema lives in weather-skills-core:
[skills/weather-skill-authoring/references/STANDARD_DATASET.md](https://github.com/rhiza-research/weather-skills-core/blob/main/skills/weather-skill-authoring/references/STANDARD_DATASET.md).
