"""Scenario discovery and loading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_ROOT = EVALS_ROOT / "scenarios"
REPO_ROOT = EVALS_ROOT.parent


@dataclass(frozen=True)
class Scenario:
    """One prompt-based composition eval."""

    id: str
    root: Path
    prompt: str
    expect: dict

    @property
    def golden(self) -> Path | None:
        for name in ("golden.py", "golden.sh"):
            path = self.root / name
            if path.exists():
                return path
        return None

    @property
    def mode(self) -> str:
        return str(self.expect.get("mode", "offline"))


def discover_scenarios() -> list[Scenario]:
    if not SCENARIOS_ROOT.is_dir():
        return []
    out = []
    for path in sorted(SCENARIOS_ROOT.iterdir()):
        if path.is_dir() and (path / "expect.json").exists() and (path / "prompt.md").exists():
            out.append(load_scenario(path.name))
    return out


def load_scenario(scenario_id: str) -> Scenario:
    root = SCENARIOS_ROOT / scenario_id
    if not root.is_dir():
        raise FileNotFoundError(f"unknown scenario {scenario_id!r} (looked in {root})")
    prompt_path = root / "prompt.md"
    expect_path = root / "expect.json"
    if not prompt_path.exists() or not expect_path.exists():
        raise FileNotFoundError(f"scenario {scenario_id!r} needs prompt.md and expect.json")
    expect = json.loads(expect_path.read_text(encoding="utf-8"))
    return Scenario(
        id=scenario_id,
        root=root,
        prompt=prompt_path.read_text(encoding="utf-8"),
        expect=expect,
    )
