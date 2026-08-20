# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "pyyaml>=6",
# ]
# ///
"""Resolve a relative date query to absolute --start-time/--end-time or --date."""

import calendar
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.availability import available_through, ecmwf_s2s_valid_init, load_products
from weather_skills_core.standard_utils import parse_date

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_LAST_RE = re.compile(r"^last-(\d+)([dwmy])$")
_NOW_RE = re.compile(r"^now-(\d+)([dwmy])$")
_QUERY_HELP = (
    "queries: latest|now|today, yesterday, last-<N>d|w|m|y, now-<N>d|w|m|y, "
    "this-week|month|year, last-week|month|year, YYYY-MM-DD, "
    "YYYY-MM-DD/YYYY-MM-DD, YYYY-MM, YYYY. "
    'Map English like "the last two weeks" to last-2w — do not pass free text.'
)
_SKILLS_DIR = Path(__file__).resolve().parents[2]


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


def _lookup(key: str | None):
    """Return (name, Availability) or None when --product is omitted."""
    if key is None:
        return None
    catalog = load_products(_SKILLS_DIR)
    if key in catalog:
        return key, catalog[key]
    kids = sorted(name for name in catalog if name.startswith(f"{key}:"))
    if kids:
        raise UsageError(
            f"--product {key!r} is not a product; pick a dataset: {', '.join(kids)}."
        )
    raise UsageError(
        f"unknown --product {key!r}. Pass the next fetcher's skill name "
        "(or skill:dataset when that fetcher has more than one clock)."
    )


def _latest(spec, as_of: date) -> date:
    if spec is None:
        return as_of
    avail = available_through(spec, as_of)
    return as_of if avail is None else avail


def _parse(query: str, as_of: date, spec) -> tuple[date, date, str, bool]:
    """Return (start, end, kind, strict). strict fails when the day is past available_through."""
    q = query.strip()
    if not q or " " in q:
        raise UsageError(f"unknown query {query!r}. {_QUERY_HELP}" if q else f"pass a query token. {_QUERY_HELP}")

    if q in {"latest", "now", "today"}:
        day = _latest(spec, as_of)
        return day, day, "point", False
    if q == "yesterday":
        day = as_of - timedelta(days=1)
        return day, day, "point", True

    m = _LAST_RE.fullmatch(q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            raise UsageError(f"{q}: N must be >= 1. {_QUERY_HELP}")
        end = _latest(spec, as_of)
        return _shift(end, n, unit, window=True), end, "range", False

    m = _NOW_RE.fullmatch(q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            raise UsageError(f"{q}: N must be >= 1. {_QUERY_HELP}")
        day = _shift(as_of, n, unit, window=False)
        return day, day, "point", True

    if q == "this-week":
        start = as_of - timedelta(days=as_of.weekday())
        return start, as_of, "range", False
    if q == "last-week":
        start = as_of - timedelta(days=as_of.weekday() + 7)
        return start, start + timedelta(days=6), "range", False
    if q == "this-month":
        return date(as_of.year, as_of.month, 1), as_of, "range", False
    if q == "last-month":
        prev = add_months(date(as_of.year, as_of.month, 1), -1)
        last = calendar.monthrange(prev.year, prev.month)[1]
        return date(prev.year, prev.month, 1), date(prev.year, prev.month, last), "range", False
    if q == "this-year":
        return date(as_of.year, 1, 1), as_of, "range", False
    if q == "last-year":
        return date(as_of.year - 1, 1, 1), date(as_of.year - 1, 12, 31), "range", False

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", q):
        day = parse_date(q)
        return day, day, "point", True
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", q)
    if m:
        start, end = parse_date(m.group(1)), parse_date(m.group(2))
        if start > end:
            raise UsageError(f"range start {start.isoformat()} is after end {end.isoformat()}.")
        return start, end, "range", False
    m = re.fullmatch(r"(\d{4})-(\d{2})", q)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if month < 1 or month > 12:
            raise UsageError(f"unknown query {query!r}. {_QUERY_HELP}")
        last = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last), "range", False
    m = re.fullmatch(r"(\d{4})", q)
    if m:
        year = int(m.group(1))
        return date(year, 1, 1), date(year, 12, 31), "range", False

    raise UsageError(f"unknown query {query!r}. {_QUERY_HELP}")


def _clip(start, end, kind, strict, query, as_of, name, spec):
    """Apply product coverage. Returns (start, end, kind, clipped, avail, note)."""
    if spec is not None and spec.shape == "date" and kind == "range":
        raise UsageError(
            f"{name} takes a single --date (forecast init), not a range. "
            f"{query} is a range. Use latest for the most recent allowed init, "
            "or pass a YYYY-MM-DD."
        )

    avail = None if spec is None else available_through(spec, as_of)
    orig_start, orig_end = start, end
    clipped = False
    earliest = None if spec is None else spec.earliest

    if earliest is not None and end < earliest:
        raise UsageError(
            f"{query} ends {end.isoformat()}, before {name} coverage starts "
            f"{earliest.isoformat()}."
        )
    if (
        spec is not None
        and spec.schedule == "ecmwf-s2s"
        and start == end
        and not ecmwf_s2s_valid_init(start)
    ):
        raise UsageError(
            f"{start.isoformat()} is not an ECMWF S2S real-time init "
            "(Mon/Thu only before 2023-06-27). Pick a Monday or Thursday, or use latest."
        )
    if strict and avail is not None and end > avail:
        raise UsageError(
            f"{query} resolves to {end.isoformat()}, which is after {name} "
            f"available_through {avail.isoformat()} (as_of {as_of.isoformat()}). "
            f"Use latest (or last-<N>*) for the most recent available window."
        )
    if avail is not None and end > avail:
        end = avail
        clipped = True
    if earliest is not None and start < earliest:
        start = earliest
        clipped = True
    if start > end:
        extra = f", available_through {avail.isoformat()}" if avail is not None else ""
        raise UsageError(
            f"{query} is empty after applying {name or 'clock'} coverage "
            f"(as_of {as_of.isoformat()}{extra})."
        )
    if kind == "point" and start != end:
        kind = "range"

    parts = [f"as_of={as_of.isoformat()}"]
    if spec is None:
        parts.append("product=none")
    else:
        parts.append(f"product={name}")
        parts.append(spec.note)
    if avail is not None:
        parts.append(f"available_through={avail.isoformat()}")
    if clipped:
        parts.append(
            f"clipped from {orig_start.isoformat()}/{orig_end.isoformat()} "
            f"to {start.isoformat()}/{end.isoformat()}"
        )
    return start, end, kind, clipped, avail, "; ".join(parts)


def _shape(kind: str, spec) -> str:
    if spec is not None and spec.shape in {"date", "range"}:
        return spec.shape
    return "date" if kind == "point" else "range"


def _list_products() -> str:
    catalog = load_products(_SKILLS_DIR)
    width = max((len(k) for k in catalog), default=7)
    width = max(width, len("PRODUCT"))
    lines = [f"{'PRODUCT':<{width}}  {'SHAPE':<8} LAG / NOTE"]
    for key in sorted(catalog):
        spec = catalog[key]
        if spec.schedule == "pentad":
            lag = "pentad (~2-7d)"
        elif spec.schedule == "ecmwf-s2s":
            lag = "2d embargo"
        elif spec.lag_days is None:
            lag = "none (future ok)"
        else:
            lag = f"{spec.lag_days}d"
        lines.append(f"{key:<{width}}  {spec.shape:<8} {lag} — {spec.note}")
    return "\n".join(lines)


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
    "--product",
    help="Fetcher product (chirps-fetch, dynamical-fetch:noaa-gfs-forecast, …)",
)
@weather_skill.argument(
    "--as-of",
    help="Clock date YYYY-MM-DD (default: today's UTC date)",
)
@weather_skill.argument(
    "--emit",
    choices=["flags", "iso", "json"],
    default="flags",
    help="stdout shape: CLI flags (default), START/END, or JSON",
)
@weather_skill.argument(
    "--list-products",
    action="store_true",
    help="Print the live product catalog (shape and lag from each fetcher SKILL.md) and exit",
)
def resolve_time(query, product, as_of, emit="flags", list_products=False, **kwargs):
    """Resolve a relative date query to absolute --start-time/--end-time or --date."""
    if list_products:
        print(_list_products())
        return
    if query is None:
        raise UsageError(f"pass a query token (or --list-products). {_QUERY_HELP}")

    as_of_date = datetime.now(UTC).date() if as_of is None else parse_date(as_of)
    looked = _lookup(product)
    name, spec = (None, None) if looked is None else looked
    start, end, kind, strict = _parse(query, as_of_date, spec)
    start, end, kind, clipped, avail, note = _clip(
        start, end, kind, strict, query.strip(), as_of_date, name, spec
    )
    print(note, file=sys.stderr)

    shape = _shape(kind, spec)
    iso_start, iso_end = start.isoformat(), end.isoformat()
    if emit == "json":
        print(
            json.dumps(
                {
                    "query": query.strip(),
                    "as_of": as_of_date.isoformat(),
                    "product": name,
                    "available_through": None if avail is None else avail.isoformat(),
                    "start_time": iso_start,
                    "end_time": iso_end,
                    "date": iso_end if shape == "date" else None,
                    "shape": shape,
                    "clipped": clipped,
                    "flags": (
                        f"--date {iso_end}"
                        if shape == "date"
                        else f"--start-time {iso_start} --end-time {iso_end}"
                    ),
                    "note": note,
                }
            )
        )
    elif emit == "iso":
        print(iso_end if shape == "date" else f"{iso_start}/{iso_end}")
    elif shape == "date":
        print(f"--date {iso_end}")
    else:
        print(f"--start-time {iso_start} --end-time {iso_end}")


if __name__ == "__main__":
    resolve_time()
