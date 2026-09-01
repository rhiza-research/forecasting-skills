# CLI flag conventions

Skills in this repo declare their CLIs through the `@weather_skill` decorator
from `weather_skills_core`, so a flag that does the same thing on different
skills shares one canonical name. Date flags take absolute `YYYY-MM-DD` only;
resolve relative dates before calling a skill.

The canonical, enforced mapping of concept to flag name lives in weather-skills-core:
[docs/weather-skill-authoring/references/CONVENTIONS.md](https://github.com/rhiza-research/weather-skills-core/blob/main/docs/weather-skill-authoring/references/CONVENTIONS.md).
