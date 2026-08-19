#!/usr/bin/env python3
"""Run prompt-based composition evals.

Examples::

    # Deterministic golden scripts (CI-safe)
    uv run --with weather-skills-core python evals/run_eval.py --agent script

    # One scenario
    uv run python evals/run_eval.py --scenario weekly-totals-offline --agent script

    # Model-in-the-loop (needs credentials / CLI)
    CURSOR_API_KEY=… uv run --with cursor-sdk python evals/run_eval.py --agent cursor
    uv run python evals/run_eval.py --agent claude --scenario weekly-totals-offline
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# Allow `python evals/run_eval.py` from repo root without install.
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from evals.harness.agents import get_backend  # noqa: E402
from evals.harness.fixtures import prepare_fixtures  # noqa: E402
from evals.harness.scenario import discover_scenarios, load_scenario  # noqa: E402
from evals.harness.score import score_workspace, write_report  # noqa: E402


def _run_one(scenario_id: str, *, agent: str, keep_workdir: Path | None) -> int:
    scenario = load_scenario(scenario_id)
    timeout_s = int(scenario.expect.get("timeout_s", 600))

    if keep_workdir is not None:
        workdir = Path(keep_workdir) / scenario.id
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        cleanup = None
    else:
        tmp = tempfile.TemporaryDirectory(prefix=f"eval-{scenario.id}-")
        workdir = Path(tmp.name)
        cleanup = tmp

    try:
        fixtures = scenario.expect.get("fixtures") or []
        if fixtures:
            prepare_fixtures(workdir, fixtures)

        backend = get_backend(agent)
        print(f"==> {scenario.id}  agent={backend.name}  workdir={workdir}", file=sys.stderr)
        agent_result = backend.run(scenario, workdir, timeout_s=timeout_s)
        if not agent_result.ok:
            print(f"agent failed: {agent_result.detail}", file=sys.stderr)
            report = score_workspace(scenario.id, workdir, scenario.expect)
            report.add("agent", False, agent_result.detail)
            report.passed = False
        else:
            report = score_workspace(scenario.id, workdir, scenario.expect)
            report.add("agent", True, agent_result.detail)

        out_report = workdir / "_eval" / "score.json"
        write_report(report, out_report)
        status = "PASS" if report.passed else "FAIL"
        print(f"{status}  {scenario.id}", file=sys.stderr)
        for check in report.checks:
            mark = "ok" if check.ok else "X "
            print(f"  [{mark}] {check.name}" + (f" — {check.detail}" if check.detail else ""), file=sys.stderr)
        print(f"report: {out_report}", file=sys.stderr)
        return 0 if report.passed else 1
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        help="Scenario id (repeatable). Default: all scenarios.",
    )
    parser.add_argument(
        "--agent",
        default="script",
        choices=["script", "cursor", "claude"],
        help="Agent backend (default: script = golden path, no LLM).",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Keep workspaces under this directory (default: temp dirs).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List scenarios and exit.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for sc in discover_scenarios():
            print(f"{sc.id}\t{sc.mode}\t{sc.root}")
        return 0

    ids = args.scenarios or [s.id for s in discover_scenarios()]
    if not ids:
        print("no scenarios found under evals/scenarios/", file=sys.stderr)
        return 2

    failures = 0
    for scenario_id in ids:
        failures += _run_one(scenario_id, agent=args.agent, keep_workdir=args.workdir)
    if len(ids) > 1:
        print(f"done: {len(ids) - failures}/{len(ids)} passed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
