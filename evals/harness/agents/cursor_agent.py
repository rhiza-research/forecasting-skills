"""Cursor SDK agent backend (optional; needs CURSOR_API_KEY)."""

from __future__ import annotations

import os
from pathlib import Path

from evals.harness.agents.base import AgentResult
from evals.harness.scenario import REPO_ROOT, Scenario


def _system_prompt() -> str:
    path = REPO_ROOT / "agents" / "forecaster.md"
    body = path.read_text(encoding="utf-8") if path.exists() else ""
    # Strip YAML frontmatter if present.
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()
    return (
        body
        + "\n\n## Eval harness constraints\n"
        "Work only inside the current working directory. Prefer composing "
        "`forecasting-skills` / `uv run` skill scripts. Reuse fixture Zarrs "
        "already present instead of fetching from the network when possible.\n"
    )


class CursorAgent:
    """Run the scenario prompt via the Cursor SDK local agent."""

    name = "cursor"

    def run(self, scenario: Scenario, workdir: Path, *, timeout_s: int) -> AgentResult:
        api_key = os.environ.get("CURSOR_API_KEY")
        if not api_key:
            return AgentResult(ok=False, detail="CURSOR_API_KEY is not set")

        try:
            from cursor_sdk import Agent, LocalAgentOptions
        except ImportError:
            return AgentResult(
                ok=False,
                detail="cursor-sdk is not installed; pip/uv add cursor-sdk for --agent cursor",
            )

        transcript = workdir / "_eval" / "transcript.txt"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        prompt = (
            f"{_system_prompt()}\n\n## Task\n\n{scenario.prompt.strip()}\n\n"
            f"Workspace: {workdir}\n"
            "Write outputs under this workspace. When finished, stop.\n"
        )
        model = os.environ.get("EVAL_CURSOR_MODEL", "composer-2.5")
        chunks: list[str] = []
        try:
            with Agent.create(
                model=model,
                api_key=api_key,
                local=LocalAgentOptions(cwd=str(workdir)),
            ) as agent:
                run = agent.send(prompt)
                for message in run.messages():
                    if getattr(message, "type", None) == "assistant":
                        content = getattr(message, "message", None)
                        blocks = getattr(content, "content", None) or []
                        for block in blocks:
                            if getattr(block, "type", None) == "text":
                                chunks.append(getattr(block, "text", ""))
                result = run.wait()
        except Exception as exc:  # noqa: BLE001
            transcript.write_text("\n".join(chunks) + f"\n\nERROR: {exc}\n", encoding="utf-8")
            return AgentResult(ok=False, detail=str(exc), transcript_path=transcript)

        transcript.write_text("\n".join(chunks) or "(no assistant text)", encoding="utf-8")
        status = getattr(result, "status", None)
        if status and str(status).lower() in {"error", "failed"}:
            return AgentResult(
                ok=False,
                detail=f"cursor agent status={status}",
                transcript_path=transcript,
            )
        return AgentResult(ok=True, detail=f"cursor status={status}", transcript_path=transcript)
