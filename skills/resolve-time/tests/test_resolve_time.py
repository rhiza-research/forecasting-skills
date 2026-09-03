"""Correctness tests for resolve-time."""

import json
from datetime import UTC, date, datetime

import pytest
from conftest import load_skill, run_skill

AS_OF = "2026-08-20"  # Thursday


@pytest.fixture(scope="module")
def mod():
    return load_skill("resolve-time", "resolve")


@pytest.fixture
def resolve_time(mod):
    return mod.resolve_time


def _flags(capsys, resolve_time, *argv):
    run_skill(resolve_time, *argv)
    captured = capsys.readouterr()
    return captured.out.strip(), captured.err.strip()


def test_last_two_weeks(capsys, resolve_time):
    out, err = _flags(capsys, resolve_time, "last-2w", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-07 --end-time 2026-08-20"
    assert err == "as_of=2026-08-20"


def test_last_14d_matches_last_2w(capsys, resolve_time):
    a, _ = _flags(capsys, resolve_time, "last-2w", "--as-of", AS_OF)
    b, _ = _flags(capsys, resolve_time, "last-14d", "--as-of", AS_OF)
    assert a == b


def test_latest_is_as_of(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "latest", "--as-of", AS_OF)
    assert out == "--date 2026-08-20"


def test_yesterday(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "yesterday", "--as-of", AS_OF)
    assert out == "--date 2026-08-19"


def test_now_3d(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "now-3d", "--as-of", AS_OF)
    assert out == "--date 2026-08-17"


def test_this_week_iso_monday(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "this-week", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-17 --end-time 2026-08-20"


def test_last_week_is_previous_iso_week(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "last-week", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-10 --end-time 2026-08-16"


def test_this_month_so_far(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "this-month", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-01 --end-time 2026-08-20"


def test_last_30d_is_rolling(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "last-30d", "--as-of", AS_OF)
    assert out == "--start-time 2026-07-22 --end-time 2026-08-20"


def test_last_month_full_calendar(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "last-month", "--as-of", AS_OF)
    assert out == "--start-time 2026-07-01 --end-time 2026-07-31"


def test_calendar_month_token(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "2026-03", "--as-of", AS_OF)
    assert out == "--start-time 2026-03-01 --end-time 2026-03-31"


def test_absolute_range_is_unclipped(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "2026-08-01/2026-08-20", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-01 --end-time 2026-08-20"


def test_future_range_is_allowed(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "2030-01-01/2030-12-31", "--as-of", AS_OF)
    assert out == "--start-time 2030-01-01 --end-time 2030-12-31"


def test_iso_and_json_formats(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "last-7d", "--as-of", AS_OF, "--emit", "iso")
    assert out == "2026-08-14/2026-08-20"

    run_skill(resolve_time, "latest", "--as-of", AS_OF, "--emit", "json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["date"] == "2026-08-20"
    assert payload["flags"] == "--date 2026-08-20"
    assert payload["time"] == "date"


def test_english_phrase_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_time, "the last two weeks", "--as-of", AS_OF)
    assert exc.value.code == 2


def test_default_as_of_is_utc_today(capsys, resolve_time, mod, monkeypatch):
    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 20, 15, 0, tzinfo=UTC)

    monkeypatch.setattr(mod, "datetime", _FakeDateTime)
    out, _ = _flags(capsys, resolve_time, "latest")
    assert out == "--date 2026-08-20"


def test_add_months_clamps_end_of_month(mod):
    assert mod.add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)


def test_rolling_last_month_from_month_end(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "last-1m", "--as-of", "2026-03-31")
    assert out == "--start-time 2026-03-01 --end-time 2026-03-31"


def test_missing_query_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_time, "--as-of", AS_OF)
    assert exc.value.code == 2
