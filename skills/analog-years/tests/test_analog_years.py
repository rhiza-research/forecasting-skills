"""Correctness tests for analog-years."""

import json

import pytest
from conftest import load_skill, run_skill
from weather_skills_core import UsageError


@pytest.fixture(scope="module")
def mod():
    return load_skill("analog-years", "analog_years")


@pytest.fixture
def analog_years(mod):
    return mod.analog_years


def _run(capsys, analog_years, *argv):
    run_skill(analog_years, *argv)
    captured = capsys.readouterr()
    return captured.out.strip(), captured.err.strip()


def test_2026_prints_stub_years(capsys, analog_years):
    out, err = _run(capsys, analog_years, "--date", "2026-09-01")
    assert out == "1982 1997 2006 2015 2019 2023"
    assert err == "year=2026"


def test_any_day_in_2026_matches(capsys, analog_years):
    a, _ = _run(capsys, analog_years, "--date", "2026-01-01")
    b, _ = _run(capsys, analog_years, "--date", "2026-12-31")
    assert a == b == "1982 1997 2006 2015 2019 2023"


def test_json_emit(capsys, analog_years):
    out, _ = _run(capsys, analog_years, "--date", "2026-03-15", "--emit", "json")
    payload = json.loads(out)
    assert payload == {
        "date": "2026-03-15",
        "year": 2026,
        "years": [1982, 1997, 2006, 2015, 2019, 2023],
    }


def test_other_year_mentions_got_year(analog_years, capsys):
    with pytest.raises(SystemExit) as exc:
        run_skill(analog_years, "--date", "2027-01-01")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "2027" in err
    assert "only implemented" in err


def test_missing_date_is_usage_error(analog_years):
    with pytest.raises(SystemExit) as exc:
        run_skill(analog_years)
    assert exc.value.code == 2


def test_analogs_for_helper(mod):
    assert mod.analogs_for(2026) == (1982, 1997, 2006, 2015, 2019, 2023)
    with pytest.raises(UsageError, match="2024"):
        mod.analogs_for(2024)
