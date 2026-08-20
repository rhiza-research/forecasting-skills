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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.availability import (
    Availability,
    ecmwf_s2s_valid_init,
)
from weather_skills_core.availability import available_through as spec_available_through
from weather_skills_core.availability import (
    load_products as load_availability_products,
)
from weather_skills_core.standard_utils import parse_date

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_LAST_RE = re.compile(r"^last-(\d+)([dwmy])$")
_NOW_RE = re.compile(r"^now-(\d+)([dwmy])$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")

_QUERY_HELP = (
    "queries: latest|now|today, yesterday, last-<N>d|w|m|y, now-<N>d|w|m|y, "
    "this-week|month|year, last-week|month|year, YYYY-MM-DD, "
    "YYYY-MM-DD/YYYY-MM-DD, YYYY-MM, YYYY. "
    'Map English like "the last two weeks" to last-2w — do not pass free text.'
)

_SKILLS_DIR = Path(__file__).resolve().parents[2]


def _utc_today() -> date:
    return datetime.now(UTC).date()


def add_months(d: date, months: int) -> date:
    """Shift a date by whole months, clamping the day into the target month."""
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def add_years(d: date, years: int) -> date:
    return add_months(d, years * 12)


def iso_week_bounds(d: date) -> tuple[date, date]:
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _rolling_start(end: date, n: int, unit: str) -> date:
    if unit == "d":
        return end - timedelta(days=n - 1)
    if unit == "w":
        return end - timedelta(days=n * 7 - 1)
    if unit == "m":
        return add_months(end, -n) + timedelta(days=1)
    return add_years(end, -n) + timedelta(days=1)


def _offset_from(origin: date, n: int, unit: str) -> date:
    if unit == "d":
        return origin - timedelta(days=n)
    if unit == "w":
        return origin - timedelta(days=n * 7)
    if unit == "m":
        return add_months(origin, -n)
    return add_years(origin, -n)


@dataclass(frozen=True)
class Product:
    """A catalog entry: skill key plus core Availability spec."""

    key: str
    spec: Availability

    @property
    def shape(self) -> str:
        return self.spec.shape

    @property
    def note(self) -> str:
        return self.spec.note

    @property
    def earliest(self) -> date | None:
        return self.spec.earliest


@lru_cache(maxsize=1)
def load_products() -> dict[str, Product]:
    """Load products from sibling fetcher SKILL.md files."""
    mapping = load_availability_products(_SKILLS_DIR)
    return {key: Product(key, spec) for key, spec in mapping.items()}


def lookup_product(key: str | None) -> Product | None:
    if key is None:
        return None
    products = load_products()
    product = products.get(key)
    if product is not None:
        return product
    prefix = f"{key}:"
    children = sorted(name for name in products if name.startswith(prefix))
    if children:
        raise UsageError(
            f"--product {key!r} is not a product; pick a dataset: {', '.join(children)}."
        )
    known = ", ".join(sorted(products))
    raise UsageError(f"unknown --product {key!r}. Known products: {known}.")


def available_through(product: Product | None, as_of: date) -> date | None:
    """Latest date the product can fill, or None when there is no realtime cap."""
    if product is None:
        return as_of
    return spec_available_through(product.spec, as_of)


def _rolling_end(product: Product | None, as_of: date) -> date:
    avail = available_through(product, as_of)
    return as_of if avail is None else avail


@dataclass(frozen=True)
class Resolved:
    start: date
    end: date
    kind: str  # "point" | "range"
    as_of: date
    product: str | None
    available_through: date | None
    query: str
    clipped: bool
    note: str


def _clip(
    start: date,
    end: date,
    *,
    product: Product | None,
    as_of: date,
    query: str,
    kind: str,
    require_available: bool,
) -> Resolved:
    if product is not None and product.shape == "date" and kind == "range":
        raise UsageError(
            f"{product.key} takes a single --date (forecast init), not a range. "
            f"{query} is a range. Use latest for the most recent allowed init, "
            "or pass a YYYY-MM-DD."
        )

    avail = available_through(product, as_of)
    clipped = False
    orig_start, orig_end = start, end

    if product is not None and product.earliest is not None and end < product.earliest:
        raise UsageError(
            f"{query} ends {end.isoformat()}, before {product.key} coverage starts "
            f"{product.earliest.isoformat()}."
        )

    if (
        product is not None
        and product.spec.schedule == "ecmwf-s2s"
        and start == end
        and not ecmwf_s2s_valid_init(start)
    ):
        raise UsageError(
            f"{start.isoformat()} is not an ECMWF S2S real-time init "
            "(Mon/Thu only before 2023-06-27). Pick a Monday or Thursday, or use latest."
        )

    if require_available and avail is not None and end > avail:
        name = product.key if product else "the product"
        raise UsageError(
            f"{query} resolves to {end.isoformat()}, which is after {name} "
            f"available_through {avail.isoformat()} (as_of {as_of.isoformat()}). "
            f"Use latest (or last-<N>*) for the most recent available window."
        )

    if avail is not None and end > avail:
        end = avail
        clipped = True
    if product is not None and product.earliest is not None and start < product.earliest:
        start = product.earliest
        clipped = True
    if start > end:
        name = product.key if product else "clock"
        extra = f", available_through {avail.isoformat()}" if avail is not None else ""
        raise UsageError(
            f"{query} is empty after applying {name} coverage (as_of {as_of.isoformat()}{extra})."
        )

    if kind == "point" and start != end:
        kind = "range"

    note_parts = [f"as_of={as_of.isoformat()}"]
    if product is not None:
        note_parts.append(f"product={product.key}")
        note_parts.append(product.note)
    else:
        note_parts.append("product=none")
    if avail is not None:
        note_parts.append(f"available_through={avail.isoformat()}")
    if clipped:
        note_parts.append(
            f"clipped from {orig_start.isoformat()}/{orig_end.isoformat()} "
            f"to {start.isoformat()}/{end.isoformat()}"
        )
    return Resolved(
        start=start,
        end=end,
        kind=kind,
        as_of=as_of,
        product=None if product is None else product.key,
        available_through=avail,
        query=query,
        clipped=clipped,
        note="; ".join(note_parts),
    )


def resolve(query: str, *, as_of: date, product: Product | None) -> Resolved:
    """Turn a query token plus as_of/product into an inclusive date window."""
    q = query.strip()
    if not q:
        raise UsageError(f"pass a query token. {_QUERY_HELP}")
    if " " in q:
        raise UsageError(f"unknown query {query!r}. {_QUERY_HELP}")

    if q in {"latest", "now", "today"}:
        end = _rolling_end(product, as_of)
        return _clip(
            end,
            end,
            product=product,
            as_of=as_of,
            query=q,
            kind="point",
            require_available=False,
        )

    if q == "yesterday":
        day = as_of - timedelta(days=1)
        return _clip(
            day,
            day,
            product=product,
            as_of=as_of,
            query=q,
            kind="point",
            require_available=True,
        )

    m = _LAST_RE.fullmatch(q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            raise UsageError(f"{q}: N must be >= 1. {_QUERY_HELP}")
        end = _rolling_end(product, as_of)
        start = _rolling_start(end, n, unit)
        return _clip(
            start,
            end,
            product=product,
            as_of=as_of,
            query=q,
            kind="range",
            require_available=False,
        )

    m = _NOW_RE.fullmatch(q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            raise UsageError(f"{q}: N must be >= 1. {_QUERY_HELP}")
        day = _offset_from(as_of, n, unit)
        return _clip(
            day,
            day,
            product=product,
            as_of=as_of,
            query=q,
            kind="point",
            require_available=True,
        )

    if q in {"this-week", "last-week", "this-month", "last-month", "this-year", "last-year"}:
        if q == "this-week":
            start, week_end = iso_week_bounds(as_of)
            end = min(as_of, week_end)
        elif q == "last-week":
            start, end = iso_week_bounds(as_of - timedelta(days=7))
        elif q == "this-month":
            start, month_end = month_bounds(as_of.year, as_of.month)
            end = min(as_of, month_end)
        elif q == "last-month":
            prev = add_months(date(as_of.year, as_of.month, 1), -1)
            start, end = month_bounds(prev.year, prev.month)
        elif q == "this-year":
            start, end = date(as_of.year, 1, 1), as_of
        else:
            start, end = date(as_of.year - 1, 1, 1), date(as_of.year - 1, 12, 31)
        return _clip(
            start,
            end,
            product=product,
            as_of=as_of,
            query=q,
            kind="range",
            require_available=False,
        )

    if _DAY_RE.fullmatch(q):
        day = parse_date(q)
        return _clip(
            day,
            day,
            product=product,
            as_of=as_of,
            query=q,
            kind="point",
            require_available=True,
        )

    m = _RANGE_RE.fullmatch(q)
    if m:
        start, end = parse_date(m.group(1)), parse_date(m.group(2))
        if start > end:
            raise UsageError(f"range start {start.isoformat()} is after end {end.isoformat()}.")
        return _clip(
            start,
            end,
            product=product,
            as_of=as_of,
            query=q,
            kind="range",
            require_available=False,
        )

    m = _MONTH_RE.fullmatch(q)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if month < 1 or month > 12:
            raise UsageError(f"unknown query {query!r}. {_QUERY_HELP}")
        start, end = month_bounds(year, month)
        return _clip(
            start,
            end,
            product=product,
            as_of=as_of,
            query=q,
            kind="range",
            require_available=False,
        )

    m = _YEAR_RE.fullmatch(q)
    if m:
        year = int(m.group(1))
        start, end = date(year, 1, 1), date(year, 12, 31)
        return _clip(
            start,
            end,
            product=product,
            as_of=as_of,
            query=q,
            kind="range",
            require_available=False,
        )

    raise UsageError(f"unknown query {query!r}. {_QUERY_HELP}")


def _emit_shape(resolved: Resolved, product: Product | None) -> str:
    if product is not None and product.shape == "date":
        return "date"
    if product is not None and product.shape == "range":
        return "range"
    return "date" if resolved.kind == "point" else "range"


def format_flags(resolved: Resolved, product: Product | None) -> str:
    if _emit_shape(resolved, product) == "date":
        return f"--date {resolved.end.isoformat()}"
    return f"--start-time {resolved.start.isoformat()} --end-time {resolved.end.isoformat()}"


def format_iso(resolved: Resolved, product: Product | None) -> str:
    if _emit_shape(resolved, product) == "date":
        return resolved.end.isoformat()
    return f"{resolved.start.isoformat()}/{resolved.end.isoformat()}"


def format_json(resolved: Resolved, product: Product | None) -> str:
    shape = _emit_shape(resolved, product)
    payload = {
        "query": resolved.query,
        "as_of": resolved.as_of.isoformat(),
        "product": resolved.product,
        "available_through": (
            None if resolved.available_through is None else resolved.available_through.isoformat()
        ),
        "start_time": resolved.start.isoformat(),
        "end_time": resolved.end.isoformat(),
        "date": resolved.end.isoformat() if shape == "date" else None,
        "shape": shape,
        "clipped": resolved.clipped,
        "flags": format_flags(resolved, product),
        "note": resolved.note,
    }
    return json.dumps(payload)


def list_products_text() -> str:
    products = load_products()
    width = max((len(k) for k in products), default=7)
    width = max(width, len("PRODUCT"))
    lines = [f"{'PRODUCT':<{width}}  {'SHAPE':<8} LAG / NOTE"]
    for key in sorted(products):
        p = products[key]
        if p.spec.schedule == "pentad":
            lag = "pentad (~2-7d)"
        elif p.spec.schedule == "ecmwf-s2s":
            lag = "2d embargo"
        elif p.spec.lag_days is None:
            lag = "none (future ok)"
        else:
            lag = f"{p.spec.lag_days}d"
        lines.append(f"{p.key:<{width}}  {p.shape:<8} {lag} — {p.note}")
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
    help="Print the product embargo catalog and exit",
)
def resolve_time(query, product, as_of, emit="flags", list_products=False, **kwargs):
    """Resolve a relative date query to absolute --start-time/--end-time or --date."""
    if list_products:
        print(list_products_text())
        return

    if query is None:
        raise UsageError(f"pass a query token (or --list-products). {_QUERY_HELP}")

    clock = _utc_today() if as_of is None else parse_date(as_of)
    prod = lookup_product(product)
    resolved = resolve(query, as_of=clock, product=prod)
    print(resolved.note, file=sys.stderr)

    if emit == "json":
        print(format_json(resolved, prod))
    elif emit == "iso":
        print(format_iso(resolved, prod))
    else:
        print(format_flags(resolved, prod))


if __name__ == "__main__":
    resolve_time()
