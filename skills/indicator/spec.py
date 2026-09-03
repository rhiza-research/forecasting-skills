"""Parse ``--rule`` into an IndicatorSpec (named alias or clause string)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from weather_skills_core import UsageError

ALIASES = {
    "icpac-onset": ("precip sum 3d >= 20 and not precip consecutive-below 1 7d within 21d"),
    "chc-onset": "precip sum 10d > 25 and precip sum 20d > 20 after 10d",
}

_AGGS = (
    "sum",
    "mean",
    "count-above",
    "count-below",
    "consecutive-above",
    "consecutive-below",
)
_COUNT_OR_CONSEC = frozenset(
    {"count-above", "count-below", "consecutive-above", "consecutive-below"}
)
_CONSEC = frozenset({"consecutive-above", "consecutive-below"})
_OPS = (">=", "<=", ">", "<")
_WINDOW_RE = re.compile(r"^(\d+)d$", re.IGNORECASE)
_SPLIT_RE = re.compile(r"\s+(and|or)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class Clause:
    negate: bool
    variable: str
    agg: str
    window: int
    op: str | None
    threshold: float | None
    daily_threshold: float | None
    after: int | None
    within: int | None


@dataclass(frozen=True)
class IndicatorSpec:
    source: str
    expanded: str
    combinator: str | None
    clauses: tuple[Clause, ...]

    def to_json(self) -> dict:
        return asdict(self)


def parse_rule(raw: str) -> IndicatorSpec:
    """Parse a named alias or a clause string joined by ``and`` / ``or``."""
    if not isinstance(raw, str) or not raw.strip():
        raise UsageError("--rule is required (a named alias or a clause string)")
    source = raw.strip()
    key = source.lower()
    expanded = ALIASES.get(key, source)
    parts = _SPLIT_RE.split(expanded.strip())
    if not parts or not parts[0].strip():
        raise UsageError(f"could not parse --rule {source!r}")
    clauses = [parts[0]]
    combinators = []
    for i in range(1, len(parts), 2):
        combinators.append(parts[i].lower())
        if i + 1 >= len(parts) or not parts[i + 1].strip():
            raise UsageError(f"could not parse --rule {source!r}")
        clauses.append(parts[i + 1])
    unique = set(combinators)
    if len(unique) > 1:
        raise UsageError(
            "mixing 'and' and 'or' in one --rule is not supported; use a single combinator"
        )
    parsed = tuple(_parse_clause(c.strip(), source) for c in clauses)
    combinator = combinators[0] if combinators else None
    return IndicatorSpec(source=source, expanded=expanded, combinator=combinator, clauses=parsed)


def _parse_window(token: str, source: str) -> int:
    match = _WINDOW_RE.fullmatch(token)
    if not match:
        raise UsageError(f"window {token!r} in --rule {source!r} must look like '8d'")
    days = int(match.group(1))
    if days < 1:
        raise UsageError(f"window must be >= 1d in --rule {source!r}")
    return days


def _parse_op(token: str, source: str) -> str:
    if token not in _OPS:
        raise UsageError(
            f"comparator {token!r} in --rule {source!r} must be one of {', '.join(_OPS)}"
        )
    return token


def _parse_clause(text: str, source: str) -> Clause:
    tokens = text.split()
    if not tokens:
        raise UsageError(
            f"empty clause in --rule {source!r}. Known aliases: {', '.join(sorted(ALIASES))}"
        )
    i = 0
    negate = False
    if tokens[i] == "not":
        negate = True
        i += 1
    if i >= len(tokens):
        raise UsageError(f"clause {text!r} in --rule {source!r} is missing a variable")

    variable = tokens[i]
    i += 1
    if i >= len(tokens) or tokens[i] not in _AGGS:
        aliases = ", ".join(sorted(ALIASES))
        raise UsageError(
            f"could not parse clause {text!r} in --rule {source!r}. "
            f"Expected '{{variable}} {{agg}} …' with agg one of {', '.join(_AGGS)}. "
            f"Known aliases: {aliases}"
        )
    agg = tokens[i]
    i += 1

    daily_threshold = None
    if agg in _COUNT_OR_CONSEC:
        if i >= len(tokens):
            raise UsageError(
                f"clause {text!r} in --rule {source!r} needs a daily threshold after {agg}"
            )
        try:
            daily_threshold = float(tokens[i])
        except ValueError as exc:
            raise UsageError(
                f"daily threshold {tokens[i]!r} in --rule {source!r} is not a number"
            ) from exc
        i += 1

    if i >= len(tokens):
        raise UsageError(f"clause {text!r} in --rule {source!r} is missing a window (e.g. 8d)")
    window = _parse_window(tokens[i], source)
    i += 1

    op = None
    threshold = None
    if agg not in _CONSEC:
        if i >= len(tokens):
            raise UsageError(
                f"clause {text!r} in --rule {source!r} needs a comparator and threshold"
            )
        op = _parse_op(tokens[i], source)
        i += 1
        if i >= len(tokens):
            raise UsageError(f"clause {text!r} in --rule {source!r} is missing a threshold")
        try:
            threshold = float(tokens[i])
        except ValueError as exc:
            raise UsageError(
                f"threshold {tokens[i]!r} in --rule {source!r} is not a number"
            ) from exc
        i += 1

    after = None
    within = None
    while i < len(tokens):
        tag = tokens[i]
        if tag not in ("after", "within"):
            raise UsageError(f"unexpected {tag!r} in clause {text!r} of --rule {source!r}")
        if i + 1 >= len(tokens):
            raise UsageError(f"{tag} in --rule {source!r} needs a window (e.g. {tag} 10d)")
        days = _parse_window(tokens[i + 1], source)
        if tag == "after":
            if after is not None:
                raise UsageError(f"repeated 'after' in clause {text!r} of --rule {source!r}")
            after = days
        else:
            if within is not None:
                raise UsageError(f"repeated 'within' in clause {text!r} of --rule {source!r}")
            within = days
        i += 2

    if after is not None and within is not None:
        raise UsageError(
            f"clause {text!r} in --rule {source!r} cannot take both 'after' and 'within'"
        )
    return Clause(
        negate=negate,
        variable=variable,
        agg=agg,
        window=window,
        op=op,
        threshold=threshold,
        daily_threshold=daily_threshold,
        after=after,
        within=within,
    )
