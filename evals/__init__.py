"""Prompt-based composition evals for weather-skills.

Scenarios live under ``evals/scenarios/<id>/``. The runner prepares a workspace,
optionally seeds offline fixtures, invokes an agent backend, then scores the
workspace against ``expect.json`` with deterministic checks (provenance, units,
shapes) — not an LLM judge.
"""

from __future__ import annotations

from evals.harness.scenario import Scenario, discover_scenarios, load_scenario
from evals.harness.score import ScoreReport, score_workspace

__all__ = [
    "Scenario",
    "ScoreReport",
    "discover_scenarios",
    "load_scenario",
    "score_workspace",
]
