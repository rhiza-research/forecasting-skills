"""Unit tests for the eval scorer (no agent)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.harness.fixtures import prepare_fixtures
from evals.harness.score import score_workspace
from evals.harness.scenario import discover_scenarios, load_scenario


def test_discover_scenarios():
    ids = {s.id for s in discover_scenarios()}
    assert "weekly-totals-offline" in ids
    assert "deaccumulate-before-aggregate" in ids
    assert "end-time-two-weeks" in ids


def test_score_missing_output(tmp_path):
    sc = load_scenario("end-time-two-weeks")
    prepare_fixtures(tmp_path, sc.expect["fixtures"])
    report = score_workspace(sc.id, tmp_path, sc.expect)
    assert not report.passed
    assert any(c.name.startswith("output:") and not c.ok for c in report.checks)


def test_load_expect_has_fixtures():
    sc = load_scenario("weekly-totals-offline")
    assert sc.expect["fixtures"][0]["kind"] == "daily_rates"
    assert "aggregate-temporal" in sc.expect["skills_used"]
