#!/usr/bin/env python3
"""Composition evals: seed fixtures → run golden/agent → score outputs.

Scenarios live in ``evals/scenarios/<id>/{prompt.md,expect.json,golden.py}``.

    python evals/run_eval.py --list
    python evals/run_eval.py --agent script
    python evals/run_eval.py --scenario weekly-totals-offline --workdir /tmp/ws
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import xarray as xr

from weather_skills_core.provenance import load_figure_history, load_history, stamp_zarr

REPO = Path(__file__).resolve().parents[1]
SCENARIOS = Path(__file__).resolve().parent / "scenarios"


# --- fixtures -----------------------------------------------------------------

def _write_zarr(ds: xr.Dataset, path: Path, skill: str = "eval-fixture") -> None:
    if path.exists():
        shutil.rmtree(path)
    stamp_zarr(
        ds,
        [{"skill": skill, "version": "0.0.0", "args": {"fixture": True}, "input": None}],
        source="evals-fixture",
    )
    ds.to_zarr(path, mode="w", consolidated=True)


def seed_fixture(workdir: Path, spec: dict) -> None:
    kind = spec["kind"]
    out = workdir / (spec.get("path") or spec["name"])
    out.parent.mkdir(parents=True, exist_ok=True)
    opts = {k: v for k, v in spec.items() if k not in ("kind", "path", "name")}

    if kind == "daily_rates":
        n_time = opts.get("n_time", 15)
        start = opts.get("start", "2026-08-16")
        name = opts.get("name", "precip")
        fill = opts.get("fill", 2.0)
        lats = opts.get("lats", (1.0, 2.0))
        lons = opts.get("lons", (36.0, 37.0))
        times = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n_time, "D"))
        ds = xr.Dataset(
            {name: (("time", "latitude", "longitude"), np.full((n_time, len(lats), len(lons)), fill))},
            coords={"time": times.astype("datetime64[ns]"), "latitude": list(lats), "longitude": list(lons)},
        )
        ds[name].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
        ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
        ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
        ds["time"].attrs.update(standard_name="time", axis="T")
        _write_zarr(ds, out)
        return

    if kind == "cumulative_forecast":
        n_step = opts.get("n_step", 14)
        init = opts.get("init", "2026-08-01")
        name = opts.get("name", "tp")
        fill = opts.get("fill", 1.0)
        steps = np.array([np.timedelta64(d, "D") for d in range(1, n_step + 1)])
        data = np.cumsum(np.full((n_step, 2, 2), fill), axis=0)
        ds = xr.Dataset(
            {name: (("step", "latitude", "longitude"), data)},
            coords={
                "time": np.datetime64(init, "ns"),
                "step": steps,
                "latitude": [1.0, 2.0],
                "longitude": [36.0, 37.0],
            },
        )
        ds[name].attrs.update(
            units="mm",
            standard_name="lwe_thickness_of_precipitation_amount",
            long_name="Total precipitation",
        )
        ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
        ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
        ds["step"].attrs.update(standard_name="forecast_period")
        ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
        _write_zarr(ds, out, skill="eval-fixture-cumulative")
        return

    raise SystemExit(f"unknown fixture kind {kind!r}")


# --- scoring ------------------------------------------------------------------

def _skills_in(workdir: Path) -> set[str]:
    skills: set[str] = set()
    for path in workdir.rglob("*"):
        hist = None
        if path.is_dir() and (path / "zarr.json").exists():
            hist = load_history(path)
        elif path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            hist = load_figure_history(path)
        if not hist:
            continue
        for entry in hist:
            if isinstance(entry, dict) and entry.get("skill"):
                skills.add(str(entry["skill"]))
    return skills


def score(workdir: Path, expect: dict) -> list[tuple[bool, str]]:
    """Return ``(ok, message)`` rows; all must be ok to pass."""
    rows: list[tuple[bool, str]] = []
    skills = _skills_in(workdir)

    for skill in expect.get("skills_used") or []:
        rows.append((skill in skills, f"skills_used:{skill} (have {sorted(skills)})"))
    for skill in expect.get("skills_forbidden") or []:
        rows.append((skill not in skills, f"skills_forbidden:{skill}"))

    for spec in expect.get("outputs") or []:
        pattern = spec["glob"]
        matches = sorted(workdir.glob(pattern))
        need = int(spec.get("min_count", 1))
        rows.append((len(matches) >= need, f"output:{pattern} found={len(matches)} need>={need}"))
        checks = spec.get("checks") or {}
        for path in matches:
            if checks.get("has_history"):
                hist = load_history(path) if path.is_dir() else (load_figure_history(path) or [])
                rows.append((bool(hist), f"{path.name}:has_history"))
            if not path.is_dir():
                continue
            try:
                ds = xr.open_zarr(path, consolidated=True)
            except Exception as exc:  # noqa: BLE001
                rows.append((False, f"{path.name}:open {exc}"))
                continue
            with ds:
                if "time_size" in checks:
                    got = int(ds.sizes.get("time", -1))
                    want = int(checks["time_size"])
                    rows.append((got == want, f"{path.name}:time_size got={got} want={want}"))
                for var, allowed in (checks.get("var_units") or {}).items():
                    allowed = allowed if isinstance(allowed, list) else [allowed]
                    units = ds[var].attrs.get("units") if var in ds.data_vars else None
                    rows.append((units in allowed, f"{path.name}:units:{var} got={units!r}"))
                if "aggregation_period" in checks:
                    want = checks["aggregation_period"]
                    got = next(
                        (ds[v].attrs.get("aggregation_period") for v in ds.data_vars
                         if ds[v].attrs.get("aggregation_period")),
                        None,
                    )
                    rows.append((got == want, f"{path.name}:aggregation_period got={got!r}"))
    return rows


# --- agents -------------------------------------------------------------------

def run_script(scenario_dir: Path, workdir: Path, timeout_s: int) -> tuple[bool, str]:
    golden = scenario_dir / "golden.py"
    if not golden.exists():
        return False, "no golden.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), str(REPO / "tests"), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, str(golden), "--workdir", str(workdir)],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    (workdir / "_eval").mkdir(exist_ok=True)
    (workdir / "_eval" / "transcript.txt").write_text(
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit={proc.returncode}\n"
    )
    if proc.returncode != 0:
        return False, f"golden exit {proc.returncode}: {proc.stderr[-400:]}"
    return True, "golden ok"


def run_cursor(prompt: str, workdir: Path, timeout_s: int) -> tuple[bool, str]:
    if not os.environ.get("CURSOR_API_KEY"):
        return False, "CURSOR_API_KEY not set"
    try:
        from cursor_sdk import Agent, LocalAgentOptions
    except ImportError:
        return False, "install cursor-sdk for --agent cursor"

    forecaster = REPO / "agents" / "forecaster.md"
    system = forecaster.read_text() if forecaster.exists() else ""
    if system.startswith("---"):
        system = system.split("---", 2)[-1].strip()
    full = f"{system}\n\n## Task\n\n{prompt}\n\nWorkspace: {workdir}\n"
    chunks: list[str] = []
    with Agent.create(
        model=os.environ.get("EVAL_CURSOR_MODEL", "composer-2.5"),
        api_key=os.environ["CURSOR_API_KEY"],
        local=LocalAgentOptions(cwd=str(workdir)),
    ) as agent:
        run = agent.send(full)
        for message in run.messages():
            if getattr(message, "type", None) == "assistant":
                for block in getattr(getattr(message, "message", None), "content", []) or []:
                    if getattr(block, "type", None) == "text":
                        chunks.append(getattr(block, "text", ""))
        result = run.wait()
    (workdir / "_eval").mkdir(exist_ok=True)
    (workdir / "_eval" / "transcript.txt").write_text("\n".join(chunks) or "(empty)")
    status = str(getattr(result, "status", ""))
    return status.lower() not in {"error", "failed"}, f"cursor status={status}"


def run_claude(prompt: str, workdir: Path, timeout_s: int) -> tuple[bool, str]:
    if shutil.which("claude") is None:
        return False, "claude CLI not on PATH"
    proc = subprocess.run(
        ["claude", "-p", prompt, "--agent", str(REPO / "agents" / "forecaster.md")],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    (workdir / "_eval").mkdir(exist_ok=True)
    (workdir / "_eval" / "transcript.txt").write_text(
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit={proc.returncode}\n"
    )
    return proc.returncode == 0, f"claude exit {proc.returncode}"


# --- runner -------------------------------------------------------------------

def list_scenarios() -> list[Path]:
    return sorted(
        p for p in SCENARIOS.iterdir()
        if p.is_dir() and (p / "expect.json").exists() and (p / "prompt.md").exists()
    )


def run_one(scenario_dir: Path, *, agent: str, keep: Path | None) -> int:
    sid = scenario_dir.name
    expect = json.loads((scenario_dir / "expect.json").read_text())
    prompt = (scenario_dir / "prompt.md").read_text()
    timeout_s = int(expect.get("timeout_s", 600))

    if keep is not None:
        workdir = keep / sid
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        tmp = None
    else:
        tmp = tempfile.TemporaryDirectory(prefix=f"eval-{sid}-")
        workdir = Path(tmp.name)

    try:
        for spec in expect.get("fixtures") or []:
            seed_fixture(workdir, spec)

        print(f"==> {sid}  agent={agent}  workdir={workdir}", file=sys.stderr)
        if agent == "script":
            ok, detail = run_script(scenario_dir, workdir, timeout_s)
        elif agent == "cursor":
            ok, detail = run_cursor(prompt, workdir, timeout_s)
        elif agent == "claude":
            ok, detail = run_claude(prompt, workdir, timeout_s)
        else:
            return 2

        rows = score(workdir, expect)
        rows.insert(0, (ok, f"agent: {detail}"))
        passed = all(ok for ok, _ in rows)
        (workdir / "_eval").mkdir(exist_ok=True)
        (workdir / "_eval" / "score.json").write_text(
            json.dumps(
                {"scenario": sid, "passed": passed, "checks": [{"ok": o, "detail": d} for o, d in rows]},
                indent=2,
            )
            + "\n"
        )
        print(("PASS" if passed else "FAIL") + f"  {sid}", file=sys.stderr)
        for ok, detail in rows:
            print(f"  [{'ok' if ok else 'X '}] {detail}", file=sys.stderr)
        return 0 if passed else 1
    finally:
        if tmp is not None:
            tmp.cleanup()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", action="append", dest="scenarios")
    p.add_argument("--agent", default="script", choices=["script", "cursor", "claude"])
    p.add_argument("--workdir", type=Path, default=None)
    p.add_argument("--list", action="store_true")
    args = p.parse_args(argv)

    if args.list:
        for s in list_scenarios():
            print(s.name)
        return 0

    dirs = list_scenarios()
    if args.scenarios:
        wanted = set(args.scenarios)
        dirs = [d for d in dirs if d.name in wanted]
        missing = wanted - {d.name for d in dirs}
        if missing:
            print(f"unknown scenarios: {sorted(missing)}", file=sys.stderr)
            return 2
    if not dirs:
        print("no scenarios in evals/scenarios/", file=sys.stderr)
        return 2

    failures = sum(run_one(d, agent=args.agent, keep=args.workdir) for d in dirs)
    if len(dirs) > 1:
        print(f"done: {len(dirs) - failures}/{len(dirs)} passed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
