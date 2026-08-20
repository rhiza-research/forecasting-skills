"""Correctness tests for submit-feedback."""

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def submit_feedback():
    return load_skill("submit-feedback", "build_url").submit_feedback


def test_build_url_prints_github_link(capsys, submit_feedback):
    run_skill(submit_feedback, "--title", "Plot bug", "--body", "Heatmap axis labels overlap.")

    out = capsys.readouterr().out
    assert "github.com/rhiza-research/forecasting-skills/issues/new" in out
    assert "title=Plot%20bug" in out


def test_overlong_body_exits_1(submit_feedback):
    body = "x" * 10000
    with pytest.raises(SystemExit) as exc:
        run_skill(submit_feedback, "--title", "Long body", "--body", body)
    assert exc.value.code == 1
