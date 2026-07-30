# CLI flag conventions

Skills in this repo declare their CLIs through the `@weather_skill` decorator
from `weather_skills_core`, so a flag that does the same thing on different
skills shares one canonical name, and `--start` / `--end` / `--date` /
`--time` share one date grammar (`YYYY-MM-DD` or `latest`).

The canonical, enforced mapping of concept to flag name, together with the date
grammar, lives in weather-skills-core:
[skills/weather-skill-authoring/references/CONVENTIONS.md](https://github.com/rhiza-research/weather-skills-core/blob/main/skills/weather-skill-authoring/references/CONVENTIONS.md).
