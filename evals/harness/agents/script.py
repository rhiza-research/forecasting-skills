"""Run a scenario's golden.py / golden.sh (deterministic, CI-safe)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from evals.harness.agents.base import AgentResult
from evals.harness.scenario import REPO_ROOT, Scenario


class ScriptAgent:
    """Execute the scenario golden script; no LLM involved."""

    name = "script"

    def run(self, scenario: Scenario, workdir: Path, *, timeout_s: int) -> AgentResult:
        golden = scenario.golden
        if golden is None:
            return AgentResult(ok=False, detail="scenario has no golden.py / golden.sh")

        env = os.environ.copy()
        env["EVAL_WORKDIR"] = str(workdir)
        env["EVAL_REPO_ROOT"] = str(REPO_ROOT)
        env["EVAL_SCENARIO"] = scenario.id
        # Ensure local packages resolve when running from repo.
        py_path = env.get("PYTHONPATH", "")
        pieces = [str(REPO_ROOT), str(REPO_ROOT / "tests"), py_path]
        env["PYTHONPATH"] = os.pathsep.join(p for p in pieces if p)

        if golden.suffix == ".py":
            cmd = [sys.executable, str(golden), "--workdir", str(workdir)]
        else:
            cmd = ["bash", str(golden), str(workdir)]

        transcript = workdir / "_eval" / "transcript.txt"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(ok=False, detail=f"golden timed out after {timeout_s}s")

        transcript.write_text(
            f"$ {' '.join(cmd)}\n\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
            f"exit={proc.returncode}\n",
            encoding="utf-8",
        )
        if proc.returncode != 0:
            return AgentResult(
                ok=False,
                detail=f"golden exit {proc.returncode}: {proc.stderr[-500:]}",
                transcript_path=transcript,
            )
        return AgentResult(ok=True, detail="golden ok", transcript_path=transcript)
