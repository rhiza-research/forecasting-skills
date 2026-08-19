"""Claude Code agent backend (optional; needs `claude` CLI)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from evals.harness.agents.base import AgentResult
from evals.harness.scenario import REPO_ROOT, Scenario


class ClaudeAgent:
    """Invoke ``claude --agent`` with the bundled forecaster agent."""

    name = "claude"

    def run(self, scenario: Scenario, workdir: Path, *, timeout_s: int) -> AgentResult:
        if shutil.which("claude") is None:
            return AgentResult(ok=False, detail="`claude` CLI not found on PATH")

        transcript = workdir / "_eval" / "transcript.txt"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        prompt_file = workdir / "_eval" / "prompt.md"
        prompt_file.write_text(scenario.prompt, encoding="utf-8")

        # Prefer the in-repo agent definition when running from a checkout.
        agent_flag = str(REPO_ROOT / "agents" / "forecaster.md")
        cmd = [
            "claude",
            "-p",
            scenario.prompt,
            "--agent",
            agent_flag,
            "--allowedTools",
            "Bash,Read,Write,Skill",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(ok=False, detail=f"claude timed out after {timeout_s}s")

        transcript.write_text(
            f"$ {' '.join(cmd)}\n\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
            f"exit={proc.returncode}\n",
            encoding="utf-8",
        )
        if proc.returncode != 0:
            return AgentResult(
                ok=False,
                detail=f"claude exit {proc.returncode}",
                transcript_path=transcript,
            )
        return AgentResult(ok=True, detail="claude ok", transcript_path=transcript)
