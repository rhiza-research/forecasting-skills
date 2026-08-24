# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
# ]
# ///
"""Resolve a relative date query to absolute --start-time/--end-time or --date."""

import calendar
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.standard_utils import parse_date

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_LAST_RE = re.compile(r"^last-(\d+)([dwmy])$")
_NOW_RE = re.compile(r"^now-(\d+)([dwmy])$")
_QUERY_HELP = (
    "queries: latest|now|today, yesterday, last-<N>d|w|m|y, now-<N>d|w|m|y, "
    "this-week|month|year, last-week|month|year, YYYY-MM-DD, "
    "YYYY-MM-DD/YYYY-MM-DD, YYYY-MM, YYYY. "
    'Map English like "the last two weeks" to last-2w — do not pass free text.'
)


def add_months(d: date, months: int) -> date:
    """Shift a date by whole months, clamping the day into the target month."""
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _shift(d: date, n: int, unit: str, *, window: bool) -> date:
    extra = 1 if window else 0
    if unit == "d":
        return d - timedelta(days=n - extra)
    if unit == "w":
        return d - timedelta(days=n * 7 - extra)
    if unit == "m":
        return add_months(d, -n) + timedelta(days=extra)
    return add_months(d, -n * 12) + timedelta(days=extra)


def _parse(query: str, as_of: date) -> tuple[date, date, str]:
    """Return (start, end, kind) against the calendar clock ``as_of``."""
    q = query.strip()
    if not q or " " in q:
        raise UsageError(
            f"unknown query {query!r}. {_QUERY_HELP}" if q else f"pass a query token. {_QUERY_HELP}"
        )

    if q in {"latest", "now", "today"}:
        return as_of, as_of, "point"
    if q == "yesterday":
        day = as_of - timedelta(days=1)
        return day, day, "point"

    m = _LAST_RE.fullmatch(q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            raise UsageError(f"{q}: N must be >= 1. {_QUERY_HELP}")
        return _shift(as_of, n, unit, window=True), as_of, "range"

    m = _NOW_RE.fullmatch(q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            raise UsageError(f"{q}: N must be >= 1. {_QUERY_HELP}")
        day = _shift(as_of, n, unit, window=False)
        return day, day, "point"

    if q == "this-week":
        start = as_of - timedelta(days=as_of.weekday())
        return start, as_of, "range"
    if q == "last-week":
        start = as_of - timedelta(days=as_of.weekday() + 7)
        return start, start + timedelta(days=6), "range"
    if q == "this-month":
        return date(as_of.year, as_of.month, 1), as_of, "range"
    if q == "last-month":
        prev = add_months(date(as_of.year, as_of.month, 1), -1)
        last = calendar.monthrange(prev.year, prev.month)[1]
        return date(prev.year, prev.month, 1), date(prev.year, prev.month, last), "range"
    if q == "this-year":
        return date(as_of.year, 1, 1), as_of, "range"
    if q == "last-year":
        return date(as_of.year - 1, 1, 1), date(as_of.year - 1, 12, 31), "range"

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", q):
        day = parse_date(q)
        return day, day, "point"
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", q)
    if m:
        start, end = parse_date(m.group(1)), parse_date(m.group(2))
        if start > end:
            raise UsageError(f"range start {start.isoformat()} is after end {end.isoformat()}.")
        return start, end, "range"
    m = re.fullmatch(r"(\d{4})-(\d{2})", q)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if month < 1 or month > 12:
            raise UsageError(f"unknown query {query!r}. {_QUERY_HELP}")
        last = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last), "range"
    m = re.fullmatch(r"(\d{4})", q)
    if m:
        year = int(m.group(1))
        return date(year, 1, 1), date(year, 12, 31), "range"

    raise UsageError(f"unknown query {query!r}. {_QUERY_HELP}")


@weather_skill(
    name="resolve-time",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument(
    "query",
    nargs="?",
    default=None,
    help="Relative date token (last-2w, latest, now-3d, YYYY-MM-DD, …)",
)
@weather_skill.argument(
    "--as-of",
    help="Clock date YYYY-MM-DD (default: today's UTC date)",
)
@weather_skill.argument(
    "--emit",
    choices=["flags", "iso", "json"],
    default="flags",
    help="stdout format: CLI flags (default), START/END, or JSON",
)
def resolve_time(query, as_of, emit="flags", **kwargs):
    """Resolve a relative date query to absolute --start-time/--end-time or --date."""
    if query is None:
        raise UsageError(f"pass a query token. {_QUERY_HELP}")
    as_of_date = datetime.now(UTC).date() if as_of is None else parse_date(as_of)
    start, end, kind = _parse(query, as_of_date)
    print(f"as_of={as_of_date.isoformat()}", file=sys.stderr)

    time_kind = "date" if kind == "point" else "range"
    iso_start, iso_end = start.isoformat(), end.isoformat()
    flags = (
        f"--date {iso_end}"
        if time_kind == "date"
        else f"--start-time {iso_start} --end-time {iso_end}"
    )
    if emit == "json":
        print(
            json.dumps(
                {
                    "query": query.strip(),
                    "as_of": as_of_date.isoformat(),
                    "start_time": iso_start,
                    "end_time": iso_end,
                    "date": iso_end if time_kind == "date" else None,
                    "time": time_kind,
                    "flags": flags,
                }
            )
        )
    elif emit == "iso":
        print(iso_end if time_kind == "date" else f"{iso_start}/{iso_end}")
    else:
        print(flags)


if __name__ == "__main__":
    resolve_time()
