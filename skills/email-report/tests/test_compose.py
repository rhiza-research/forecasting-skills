"""Correctness tests for email-report."""

from pathlib import Path

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def compose():
    return load_skill("email-report", "compose").compose


def test_compose_writes_eml(tmp_path, compose):
    out = tmp_path / "report.eml"

    run_skill(
        compose,
        "-o",
        str(out),
        "--from",
        "sender@example.com",
        "--to",
        "recipient@example.com",
        "--subject",
        "Weekly report",
        "--body",
        "Rainfall was above normal.",
    )

    assert Path(out).exists()
    text = out.read_text()
    assert "From: sender@example.com" in text
    assert "To: recipient@example.com" in text
    assert "Subject: Weekly report" in text
    assert "Rainfall was above normal." in text


def test_compose_body_file(tmp_path, compose):
    body_path = tmp_path / "body.txt"
    body_path.write_text("Attached findings follow.")
    out = tmp_path / "report.eml"

    run_skill(
        compose,
        "-o",
        str(out),
        "--from",
        "a@b.com",
        "--to",
        "c@d.com",
        "--subject",
        "Findings",
        "--body-file",
        str(body_path),
    )

    assert "Attached findings follow." in out.read_text()
