# Weather-Skills Envelope

The skills in this repo consume and produce a shared Zarr-based container: a
CF-compliant store with canonical gridded and station shapes, `weather_skills_*`
attributes, and the append-only `weather_skills_history` provenance chain that
every envelope Zarr and plot PNG carries.

The canonical, enforced definition of the envelope shape, its attributes, and
the `weather_skills_history` schema lives in weather-skills-core:
[skills/weather-skill-authoring/references/ENVELOPE.md](https://github.com/rhiza-research/weather-skills-core/blob/main/skills/weather-skill-authoring/references/ENVELOPE.md).
