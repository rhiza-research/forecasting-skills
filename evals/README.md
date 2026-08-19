# Prompt-based composition evals

Harness for testing how weather-skills are **composed** (by a golden script or
by a model), not for unit-testing individual skill math (that stays in
`skills/*/tests`).

## Layout

```
evals/
  run_eval.py                 # CLI entrypoint
  harness/                    # scenario load, fixtures, score, agent backends
  scenarios/<id>/
    prompt.md                 # natural-language task for the agent
    expect.json               # deterministic checks
    golden.py                 # reference composition (used by --agent script)
  tests/                      # scorer unit tests
```

## Quick start

From the repo root (with the project venv / `uv sync --group dev`):

```bash
# List scenarios
python evals/run_eval.py --list

# Run all scenarios with golden scripts (no LLM, CI-safe)
python evals/run_eval.py --agent script

# Keep workspaces for inspection
python evals/run_eval.py --agent script --workdir /tmp/ws-evals

# One scenario
python evals/run_eval.py --scenario weekly-totals-offline --agent script
```

## Agent backends

| `--agent` | Requirements | Role |
| --- | --- | --- |
| `script` (default) | none | Runs each scenario's `golden.py` — proves scoring + fixtures |
| `cursor` | `CURSOR_API_KEY`, `cursor-sdk` | Cursor SDK local agent + `agents/forecaster.md` |
| `claude` | `claude` CLI on PATH | Claude Code with the in-repo forecaster agent |

Model backends write into a fresh workspace seeded with offline fixtures from
`expect.json`. Scoring uses provenance chains and Zarr metadata — not an LLM
judge.

```bash
uv run --with cursor-sdk python evals/run_eval.py --agent cursor --scenario end-time-two-weeks
```

## expect.json (sketch)

- `fixtures` — offline Zarr seeds (`daily_rates`, `cumulative_forecast`, …)
- `skills_used` / `skills_forbidden` — must / must-not appear in any artifact's
  `weather_skills_history`
- `outputs[].glob` + `checks` — `has_history`, `time_size`, `var_units`, …

## Adding a scenario

1. Create `evals/scenarios/<id>/{prompt.md,expect.json,golden.py}`.
2. Prefer **offline** fixtures so PR CI stays credential-free.
3. Encode one composition lesson (e.g. deaccumulate before aggregate, drop
   incomplete weeks, `--end-time` alignment).
4. Run `python evals/run_eval.py --scenario <id> --agent script`.

Live/network scenarios can be added later with `"mode": "live"` and empty
fixtures; keep them out of default CI.
