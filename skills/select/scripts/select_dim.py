# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "pandas",
# ]
# ///
"""Select entries along one named dimension of a weather-skills envelope Zarr.

Picks entries along one ``--dim`` either by integer position (``--index``,
repeatable, negative positions count from the end) or by coordinate value
(``--value``, repeatable, parsed against the coord's dtype), writing a new
envelope. A single selection collapses the dim and also drops every coordinate
variable the selection leaves scalar (the dim's own coord and any auxiliary
coord on the collapsed dim), so outputs from different sources align under
``concat`` (e.g. pick the same forecast week from several model envelopes,
then merge them along a new ``model`` dim). Multiple selections keep the dim
with just those entries, in the order given. All untouched dims, coords, data
variables, and attrs pass through unchanged.
"""

import re
from pathlib import Path

from weather_skills_core import UsageError, WroteSummary, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.6"

# Strict integer-position grammar: an optional leading minus and ASCII digits
# only. Floats, whitespace, "+" signs, underscore separators, and non-ASCII
# digits are rejected.
_INDEX_RE = re.compile(r"-?[0-9]+")

# Strict numeric --value grammar: optional leading minus, ASCII-digit integer
# or decimal/scientific float. Underscore separators, nan, and inf are
# rejected (int()/float() would accept them).
_NUM_INT_RE = re.compile(r"-?[0-9]+")
_NUM_FLOAT_RE = re.compile(r"-?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?")


def _fmt_coord_value(v) -> str:
    """Render one coord element for an error-message sample."""
    import numpy as np
    import pandas as pd

    if isinstance(v, np.datetime64):
        return str(np.datetime_as_string(v, unit="s"))
    if isinstance(v, np.timedelta64):
        return str(pd.Timedelta(v))
    return str(v)


def _fail_parse(raw: str, dim: str, dtype, expected: str) -> None:
    raise UsageError(
        f"--value '{raw}' is not parseable for coord '{dim}' (dtype {dtype}); expected {expected}."
    )


def _parse_value(raw: str, coord_vals, dim: str):
    """Parse a ``--value`` literal against the coord's dtype family.

    Returns the parsed value to compare (exactly) against the coord's values,
    or exits 2 with a message naming the dtype and the expected literal form.
    """
    import numpy as np
    import pandas as pd

    dtype = coord_vals.dtype
    if np.issubdtype(dtype, np.datetime64):
        # Gate the literal before np.datetime64 sees it: relative keywords
        # ("now"/"today") and the empty string are not fixed instants, and a
        # timezone suffix (trailing Z or a +HH:MM/-HH:MM-style offset after
        # the time part) would be silently converted to UTC with a numpy
        # UserWarning. Only naive ISO literals reach the parser.
        expected = (
            "a naive ISO datetime string without timezone (e.g. 2026-06-01 or 2026-06-01T06:00)"
        )
        if not raw or raw.lower() in {"now", "today"}:
            _fail_parse(raw, dim, dtype, expected)
        time_part = re.split(r"[T ]", raw, maxsplit=1)
        if raw[-1] in "Zz" or (len(time_part) == 2 and re.search(r"[+-]", time_part[1])):
            _fail_parse(raw, dim, dtype, expected)
        try:
            return np.datetime64(raw)
        except ValueError:
            _fail_parse(raw, dim, dtype, expected)
    if np.issubdtype(dtype, np.timedelta64):
        try:
            return pd.Timedelta(raw).to_timedelta64()
        except ValueError:
            _fail_parse(raw, dim, dtype, "a pandas-style timedelta string (e.g. 7D or 168h)")
    if np.issubdtype(dtype, np.number):
        # Strict decimal grammar: int()/float() accept underscore separators
        # and nan/inf spellings, which are not coordinate literals.
        if _NUM_INT_RE.fullmatch(raw):
            return int(raw)
        if _NUM_FLOAT_RE.fullmatch(raw):
            return float(raw)
        _fail_parse(
            raw,
            dim,
            dtype,
            "an int or float literal (ASCII digits; no underscores, nan, or inf)",
        )
    if dtype.kind == "U":
        return raw
    if dtype.kind == "S":
        return raw.encode()
    if dtype.kind == "O":
        # Object-dtype string coords (e.g. a station_id axis read back from
        # zarr) match verbatim; any other object payload has no literal form.
        if coord_vals.size and isinstance(coord_vals.flat[0], str):
            return raw
        elem = type(coord_vals.flat[0]).__name__ if coord_vals.size else "unknown"
        raise UsageError(
            f"coord '{dim}' has object dtype with {elem} values, which --value "
            "cannot match against; select by position with --index instead."
        )
    raise UsageError(
        f"coord '{dim}' has dtype {dtype}, which --value cannot match against; "
        "select by position with --index instead."
    )


def _validate_args(args):
    # An existing output must be a directory (a zarr store to replace); the
    # write path replaces a directory, not a plain file.
    out = Path(args.output)
    if out.exists() and not out.is_dir():
        raise UsageError(f"--output {args.output} exists and is not a directory.")
    # Structural pass on --index before anything else: every position must
    # satisfy the strict integer grammar. The positional (range/duplicate)
    # pass runs against the opened dataset, after the cache check.
    if args.index is not None:
        for raw in args.index:
            if not _INDEX_RE.fullmatch(raw):
                raise UsageError(
                    f"--index '{raw}' is not an integer position; expected an "
                    "optionally negative ASCII-digit integer (e.g. 0, 2, -1)."
                )


def _normalize_args(args):
    # The canonical int forms (not the raw strings) are what provenance
    # records — "0" and "00" name the same selection, so they must share one
    # cache identity. Given order is preserved.
    if args.get("index") is not None:
        args["index"] = [int(raw) for raw in args["index"]]
    return args


@weather_skill(
    "select",
    _SKILL_VERSION,
    input_type="any",
    # The output shape depends on the input's shape and the selection (a
    # single selection collapses the dim), so the union declares every zarr
    # envelope shape; the returned dataset's detected shape is validated
    # against it before the write.
    output_type=("gridded", "forecast", "station"),
    extra_args={
        "dim": {"required": True, "help": "Dimension to select along (exactly one)."},
        "index": {
            "repeat": True,
            "help": "Integer position to select (repeat once per position; negative positions "
            "count from the end). Mutually exclusive with --value.",
        },
        "value": {
            "repeat": True,
            "help": "Coordinate value to select, parsed against the coord's dtype (repeat once "
            "per value). Mutually exclusive with --index.",
        },
    },
    mutex_groups={
        "selector": {"args": ("index", "value"), "required": True},
    },
    validate_args=_validate_args,
    normalize_args=_normalize_args,
)
def select(ds, dim, index, value):
    """Select entries along one named dimension of a weather-skills envelope Zarr."""
    import numpy as np

    if dim not in ds.dims:
        raise UsageError(f"--dim '{dim}' not in dims {list(ds.dims)}.")
    size = ds.sizes[dim]

    if index is not None:
        # Positional checks; the structural grammar pass already ran before
        # the cache check.
        positions_given = [int(raw) for raw in index]
        given = list(zip(index, positions_given, strict=True))
        for raw, pos in given:
            if pos < -size or pos >= size:
                raise UsageError(
                    f"--index {raw} is out of range for dim '{dim}' of "
                    f"size {size} (valid positions {-size}..{size - 1})."
                )
        # Two positions addressing the same element — including a negative
        # alias of an already-given position — are an error: pos % size
        # canonicalizes both signs to one position.
        seen: dict = {}
        for raw, pos in given:
            canon = pos % size
            if canon in seen:
                raise UsageError(
                    f"--index {seen[canon]} and --index {raw} address the same "
                    f"element (position {canon}) of dim '{dim}' (size {size})."
                )
            seen[canon] = raw
        positions = [pos for _, pos in given]
    else:
        # Structural: --value needs a coordinate variable on the dim to match
        # against; a coord-less dim can only be addressed by position.
        if dim not in ds.coords:
            raise UsageError(
                f"dim '{dim}' has no coordinate variable, so --value has "
                "nothing to match against; select by position with --index instead."
            )
        coord_vals = ds[dim].values
        positions = []
        seen = {}
        for raw in value:
            parsed = _parse_value(raw, coord_vals, dim)
            matched = np.nonzero(coord_vals == parsed)[0]
            if matched.size == 0:
                sample = ", ".join(_fmt_coord_value(v) for v in coord_vals[:8])
                more = "" if size <= 8 else f", ... ({size} values total)"
                raise UsageError(
                    f"--value '{raw}' not found in coord '{dim}'; available values: {sample}{more}."
                )
            if matched.size > 1:
                raise UsageError(
                    f"--value '{raw}' is ambiguous: it matches {matched.size} "
                    f"entries of coord '{dim}' (positions {matched.tolist()}); "
                    "use --index to pick one."
                )
            pos = int(matched[0])
            if pos in seen:
                raise UsageError(
                    f"--value '{seen[pos]}' and --value '{raw}' address the same "
                    f"element (position {pos}) of dim '{dim}'."
                )
            seen[pos] = raw
            positions.append(pos)

    if len(positions) == 1:
        # A single selection collapses the dim. Also drop every coordinate
        # variable the selection left scalar — the dim's own coord and any
        # auxiliary coord on the collapsed dim (e.g. valid_time(step)): a
        # scalar coord differing between two selected outputs would block or
        # pollute their concat along a new dim. Coords that were already
        # scalar on the input pass through untouched.
        pre_scalar = {c for c in ds.coords if ds[c].ndim == 0}
        out_ds = ds.isel({dim: positions[0]})
        newly_scalar = [c for c in out_ds.coords if out_ds[c].ndim == 0 and c not in pre_scalar]
        out_ds = out_ds.drop_vars(newly_scalar)
    else:
        # Multiple selections keep the dim with just those entries, in the
        # order given on the command line.
        out_ds = ds.isel({dim: positions})

    return out_ds, WroteSummary(f"{out_ds.sizes}", replace=True)


if __name__ == "__main__":
    select()
