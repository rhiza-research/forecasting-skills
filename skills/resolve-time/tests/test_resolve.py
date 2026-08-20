"""Correctness tests for resolve-time."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

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


def test_last_two_weeks_no_product(capsys, resolve_time):
    out, err = _flags(capsys, resolve_time, "last-2w", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-07 --end-time 2026-08-20"
    assert "as_of=2026-08-20" in err
    assert "product=none" in err


def test_last_two_weeks_chirps_pentad(capsys, resolve_time):
    # as_of Aug 20 → last closed published pentad is Aug 15; 14 days back is Aug 2.
    out, err = _flags(
        capsys,
        resolve_time,
        "last-2w",
        "--product",
        "chirps-fetch",
        "--as-of",
        AS_OF,
    )
    assert out == "--start-time 2026-08-02 --end-time 2026-08-15"
    assert "available_through=2026-08-15" in err


def test_last_14d_matches_last_2w(capsys, resolve_time):
    a, _ = _flags(capsys, resolve_time, "last-2w", "--as-of", AS_OF)
    b, _ = _flags(capsys, resolve_time, "last-14d", "--as-of", AS_OF)
    assert a == b


def test_latest_ecmwf_embargo(capsys, resolve_time):
    out, err = _flags(
        capsys,
        resolve_time,
        "latest",
        "--product",
        "ecmwf-fetch",
        "--as-of",
        AS_OF,
    )
    assert out == "--date 2026-08-18"
    assert "available_through=2026-08-18" in err


def test_ecmwf_range_query_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_time, "last-2w", "--product", "ecmwf-fetch", "--as-of", AS_OF)
    assert exc.value.code == 2


def test_yesterday_inside_ecmwf_embargo_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_time, "yesterday", "--product", "ecmwf-fetch", "--as-of", AS_OF)
    assert exc.value.code == 2


def test_now_2d_ecmwf_is_latest_allowed(capsys, resolve_time):
    out, _ = _flags(
        capsys,
        resolve_time,
        "now-2d",
        "--product",
        "ecmwf-fetch",
        "--as-of",
        AS_OF,
    )
    assert out == "--date 2026-08-18"


def test_ecmwf_pre_daily_snaps_to_monday_or_thursday(capsys, resolve_time):
    # as_of Tue 2023-06-20 → embargo clock Sun 18 → snap to Thu 15.
    out, _ = _flags(
        capsys,
        resolve_time,
        "latest",
        "--product",
        "ecmwf-fetch",
        "--as-of",
        "2023-06-20",
    )
    assert out == "--date 2023-06-15"


def test_ecmwf_explicit_tuesday_before_daily_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            resolve_time,
            "2023-06-20",
            "--product",
            "ecmwf-fetch",
            "--as-of",
            "2023-06-25",
        )
    assert exc.value.code == 2


def test_this_week_iso_monday(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "this-week", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-17 --end-time 2026-08-20"


def test_last_week_is_previous_iso_week(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "last-week", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-10 --end-time 2026-08-16"


def test_this_month_so_far(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "this-month", "--as-of", AS_OF)
    assert out == "--start-time 2026-08-01 --end-time 2026-08-20"


def test_last_month_full_calendar(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "last-month", "--as-of", AS_OF)
    assert out == "--start-time 2026-07-01 --end-time 2026-07-31"


def test_calendar_month_token(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "2026-03", "--as-of", AS_OF)
    assert out == "--start-time 2026-03-01 --end-time 2026-03-31"


def test_absolute_range_clipped_to_chirps(capsys, resolve_time):
    out, err = _flags(
        capsys,
        resolve_time,
        "2026-08-01/2026-08-20",
        "--product",
        "chirps-fetch",
        "--as-of",
        AS_OF,
    )
    assert out == "--start-time 2026-08-01 --end-time 2026-08-15"
    assert "clipped" in err


def test_imerg_late_lag(capsys, resolve_time):
    out, _ = _flags(
        capsys,
        resolve_time,
        "latest",
        "--product",
        "imerg-fetch",
        "--as-of",
        AS_OF,
    )
    assert out == "--start-time 2026-08-16 --end-time 2026-08-16"


def test_chirps_latest_is_one_day_range(capsys, resolve_time):
    out, _ = _flags(
        capsys,
        resolve_time,
        "latest",
        "--product",
        "chirps-fetch",
        "--as-of",
        AS_OF,
    )
    assert out == "--start-time 2026-08-15 --end-time 2026-08-15"


def test_iso_and_json_formats(capsys, resolve_time):
    out, _ = _flags(
        capsys,
        resolve_time,
        "last-7d",
        "--as-of",
        AS_OF,
        "--emit",
        "iso",
    )
    assert out == "2026-08-14/2026-08-20"

    run_skill(
        resolve_time,
        "latest",
        "--product",
        "ecmwf-fetch",
        "--as-of",
        AS_OF,
        "--emit",
        "json",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["date"] == "2026-08-18"
    assert payload["flags"] == "--date 2026-08-18"
    assert payload["shape"] == "date"


def test_english_phrase_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_time, "the last two weeks", "--as-of", AS_OF)
    assert exc.value.code == 2


def test_unknown_product_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_time, "latest", "--product", "no-such-fetch", "--as-of", AS_OF)
    assert exc.value.code == 2


def test_list_products(capsys, resolve_time):
    out, _ = _flags(capsys, resolve_time, "--list-products")
    assert "chirps-fetch" in out
    assert "ecmwf-fetch" in out
    assert "pentad" in out
    assert "dynamical-fetch:noaa-gfs-forecast" in out
    assert "dynamical-fetch:noaa-gfs-analysis" in out
    products = load_skill("resolve-time", "resolve").load_products()
    assert "dynamical-fetch" not in products


def test_live_catalog_loads_sibling_skills():
    from weather_skills_core.availability import load_products

    skills = Path(__file__).resolve().parents[2]
    products = load_products(skills)
    assert "chirps-fetch" in products
    assert products["chirps-fetch"].schedule == "pentad"


def test_dynamical_forecast_latest_is_date(capsys, resolve_time):
    out, _ = _flags(
        capsys,
        resolve_time,
        "latest",
        "--product",
        "dynamical-fetch:noaa-gfs-forecast",
        "--as-of",
        AS_OF,
    )
    assert out == "--date 2026-08-19"


def test_dynamical_forecast_range_query_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            resolve_time,
            "last-2w",
            "--product",
            "dynamical-fetch:noaa-gfs-forecast",
            "--as-of",
            AS_OF,
        )
    assert exc.value.code == 2


def test_dynamical_analysis_range_flags(capsys, resolve_time):
    out, _ = _flags(
        capsys,
        resolve_time,
        "last-2w",
        "--product",
        "dynamical-fetch:noaa-gfs-analysis",
        "--as-of",
        AS_OF,
    )
    assert out == "--start-time 2026-08-06 --end-time 2026-08-19"


def test_bare_dynamical_fetch_lists_datasets(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_time, "latest", "--product", "dynamical-fetch", "--as-of", AS_OF)
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


def test_cmip6_keeps_future_dates(capsys, resolve_time):
    out, _ = _flags(
        capsys,
        resolve_time,
        "2030-01-01/2030-12-31",
        "--product",
        "cmip6-fetch",
        "--as-of",
        AS_OF,
    )
    assert out == "--start-time 2030-01-01 --end-time 2030-12-31"


def test_range_after_coverage_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            resolve_time,
            "2026-08-19/2026-08-20",
            "--product",
            "chirps-fetch",
            "--as-of",
            AS_OF,
        )
    assert exc.value.code == 2


def test_this_week_empty_under_chirps_lag(resolve_time):
    # ISO week starting Aug 17 is entirely after CHIRPS available_through Aug 15.
    with pytest.raises(SystemExit) as exc:
        run_skill(
            resolve_time,
            "this-week",
            "--product",
            "chirps-fetch",
            "--as-of",
            AS_OF,
        )
    assert exc.value.code == 2


def test_missing_query_is_usage_error(resolve_time):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_time, "--as-of", AS_OF)
    assert exc.value.code == 2
