"""Deterministic scoring of an eval workspace against expect.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import xarray as xr

from weather_skills_core.provenance import load_figure_history, load_history


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ScoreReport:
    scenario_id: str
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, ok=ok, detail=detail))
        if not ok:
            self.passed = False

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario_id,
            "passed": self.passed,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def _histories_in_workdir(workdir: Path) -> list[tuple[Path, list]]:
    found = []
    for path in sorted(workdir.rglob("*")):
        if path.is_dir() and (path / "zarr.json").exists():
            hist = load_history(path)
            if hist:
                found.append((path, hist))
        elif path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            hist = load_figure_history(path)
            if hist:
                found.append((path, hist))
    return found


def _skills_in_histories(histories: list[tuple[Path, list]]) -> set[str]:
    skills = set()
    for _, hist in histories:
        for entry in hist:
            if isinstance(entry, dict) and entry.get("skill"):
                skills.add(str(entry["skill"]))
    return skills


def _match_glob(workdir: Path, pattern: str) -> list[Path]:
    # Path.glob does not accept absolute patterns; keep relative to workdir.
    return sorted(p for p in workdir.glob(pattern) if p.exists())


def _check_output(workdir: Path, spec: dict, report: ScoreReport) -> None:
    pattern = spec["glob"]
    matches = _match_glob(workdir, pattern)
    min_count = int(spec.get("min_count", 1))
    label = f"output:{pattern}"
    if len(matches) < min_count:
        report.add(label, False, f"found {len(matches)}, need >= {min_count}")
        return
    report.add(label, True, f"found {len(matches)}")

    checks = spec.get("checks") or {}
    for path in matches:
        _check_one_artifact(path, checks, report)


def _check_one_artifact(path: Path, checks: dict, report: ScoreReport) -> None:
    prefix = path.name
    if checks.get("has_history"):
        if path.is_dir():
            hist = load_history(path)
        else:
            hist = load_figure_history(path) or []
        report.add(f"{prefix}:has_history", bool(hist), f"entries={len(hist or [])}")

    if not path.is_dir():
        return

    try:
        ds = xr.open_zarr(path, consolidated=True)
    except Exception as exc:  # noqa: BLE001
        report.add(f"{prefix}:open", False, str(exc))
        return

    with ds:
        if "time_size" in checks:
            want = int(checks["time_size"])
            got = int(ds.sizes.get("time", -1))
            report.add(f"{prefix}:time_size", got == want, f"got {got}, want {want}")

        if "step_size" in checks:
            want = int(checks["step_size"])
            got = int(ds.sizes.get("step", -1))
            report.add(f"{prefix}:step_size", got == want, f"got {got}, want {want}")

        var_units = checks.get("var_units") or {}
        for var, allowed in var_units.items():
            if var not in ds.data_vars:
                report.add(f"{prefix}:var:{var}", False, "missing variable")
                continue
            units = ds[var].attrs.get("units")
            allowed_list = allowed if isinstance(allowed, list) else [allowed]
            ok = units in allowed_list
            report.add(f"{prefix}:units:{var}", ok, f"got {units!r}, allow {allowed_list}")

        if checks.get("aggregation_period"):
            want = checks["aggregation_period"]
            # Any data var may carry it (pre-totals); after totals it is removed.
            got = None
            for name in ds.data_vars:
                got = ds[name].attrs.get("aggregation_period")
                if got:
                    break
            report.add(
                f"{prefix}:aggregation_period",
                got == want,
                f"got {got!r}, want {want!r}",
            )


def score_workspace(scenario_id: str, workdir: Path, expect: dict) -> ScoreReport:
    """Score ``workdir`` against a scenario's ``expect`` dict."""
    workdir = Path(workdir)
    report = ScoreReport(scenario_id=scenario_id, passed=True)

    histories = _histories_in_workdir(workdir)
    skills = _skills_in_histories(histories)
    report.add("histories_found", bool(histories) or not expect.get("skills_used"), f"n={len(histories)}")

    for skill in expect.get("skills_used") or []:
        report.add(f"skills_used:{skill}", skill in skills, f"have {sorted(skills)}")

    for group in expect.get("skills_used_any_of") or []:
        ok = any(s in skills for s in group)
        report.add(f"skills_any_of:{group}", ok, f"have {sorted(skills)}")

    for skill in expect.get("skills_forbidden") or []:
        report.add(f"skills_forbidden:{skill}", skill not in skills, f"have {sorted(skills)}")

    for out_spec in expect.get("outputs") or []:
        _check_output(workdir, out_spec, report)

    return report


def write_report(report: ScoreReport, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
