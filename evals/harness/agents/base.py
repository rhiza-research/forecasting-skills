"""Agent backend protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evals.harness.scenario import Scenario


@dataclass
class AgentResult:
    ok: bool
    detail: str = ""
    transcript_path: Path | None = None


class AgentBackend(Protocol):
    name: str

    def run(self, scenario: Scenario, workdir: Path, *, timeout_s: int) -> AgentResult:
        """Execute the scenario prompt against ``workdir``."""
