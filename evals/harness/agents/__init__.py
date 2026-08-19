"""Agent backends for the eval runner."""

from __future__ import annotations

from evals.harness.agents.base import AgentBackend, AgentResult
from evals.harness.agents.claude_agent import ClaudeAgent
from evals.harness.agents.cursor_agent import CursorAgent
from evals.harness.agents.script import ScriptAgent

BACKENDS = {
    "script": ScriptAgent,
    "cursor": CursorAgent,
    "claude": ClaudeAgent,
}


def get_backend(name: str) -> AgentBackend:
    if name not in BACKENDS:
        raise ValueError(f"unknown agent backend {name!r}; choose from {sorted(BACKENDS)}")
    return BACKENDS[name]()


__all__ = ["AgentBackend", "AgentResult", "BACKENDS", "get_backend"]
