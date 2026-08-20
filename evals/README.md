# Composition evals

Offline scenarios that check skill **composition** (not unit math).

```
evals/
  run_eval.py              # fixtures + agent + scorer (one file)
  scenarios/<id>/
    prompt.md              # task for a model agent
    expect.json            # deterministic checks
    golden.py              # reference pipeline (--agent script)
```

```bash
python evals/run_eval.py --list
python evals/run_eval.py --agent script                 # CI-safe
python evals/run_eval.py --scenario weekly-totals-offline --workdir /tmp/ws

# Optional model backends
CURSOR_API_KEY=… uv run --with cursor-sdk python evals/run_eval.py --agent cursor
python evals/run_eval.py --agent claude --scenario end-time-two-weeks
```

Scoring reads `weather_skills_history`, units, and shapes from the workspace —
not an LLM judge. Add a scenario by dropping a new folder under `scenarios/`.
