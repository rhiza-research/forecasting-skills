# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "pandas",
# ]
# ///
"""Select along one dim by --index or --value."""

import re

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_KEEP_SCALAR_COORDS = frozenset(
    {
        "station_id",
        "point_id",
        "name",
        "latitude",
        "lat",
        "longitude",
        "lon",
        "country",
    }
)
_INDEX_RE = re.compile(r"-?[0-9]+")
_NUM_INT_RE = re.compile(r"-?[0-9]+")
_NUM_FLOAT_RE = re.compile(r"-?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?")


def _is_string_coord(dtype, coord_vals) -> bool:
    """True for unicode, bytes, NumPy 2 StringDType (kind 'T'), or object-of-str."""
    if dtype.kind in "UST":
        return True
    return dtype.kind == "O" and coord_vals.size and isinstance(coord_vals.flat[0], str)


def _parse_value(raw, coord_vals, dim):
    import numpy as np
    import pandas as pd

    dtype = coord_vals.dtype
    if np.issubdtype(dtype, np.datetime64):
        # Reject relative/timezone forms — numpy would silently convert them
        if not raw or raw.lower() in {"now", "today"} or raw[-1:] in "Zz":
            raise UsageError(f"--value '{raw}' not a naive ISO datetime")
        parts = re.split(r"[T ]", raw, maxsplit=1)
        if len(parts) == 2 and re.search(r"[+-]", parts[1]):
            raise UsageError(f"--value '{raw}' not a naive ISO datetime")
        return np.datetime64(raw)
    if np.issubdtype(dtype, np.timedelta64):
        return pd.Timedelta(raw).to_timedelta64()
    if np.issubdtype(dtype, np.number):
        if _NUM_INT_RE.fullmatch(raw):
            return int(raw)
        if _NUM_FLOAT_RE.fullmatch(raw):
            return float(raw)
        raise UsageError(f"--value '{raw}' not a number literal")
    if _is_string_coord(dtype, coord_vals):
        return raw.encode() if dtype.kind == "S" else raw
    raise UsageError(f"coord '{dim}' dtype {dtype} needs --index, not --value")


@weather_skill(
    name="select",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--dim", required=True)
@weather_skill.argument("--index", action="append", help="Integer position (repeatable).")
@weather_skill.argument("--value", action="append", help="Coord value (repeatable).")
def select(ds, dim, index, value, **kwargs):
    """Select along one dim by --index or --value."""
    import numpy as np

    if (index is None) == (value is None):
        raise UsageError("exactly one of --index or --value is required")
    size = ds.sizes[dim]
    if index is not None:
        positions = []
        seen = {}
        for raw in index:
            if not _INDEX_RE.fullmatch(raw):
                raise UsageError(f"--index '{raw}' is not an integer")
            pos = int(raw)
            canon = pos % size
            if canon in seen:
                raise UsageError(f"--index {seen[canon]} and {raw} address the same element")
            seen[canon] = raw
            positions.append(pos)
    else:
        coord_vals = ds[dim].values
        positions, seen = [], {}
        for raw in value:
            matched = np.nonzero(coord_vals == _parse_value(raw, coord_vals, dim))[0]
            if matched.size != 1:
                raise UsageError(f"--value '{raw}' matched {matched.size} entries on '{dim}'")
            pos = int(matched[0])
            if pos in seen:
                raise UsageError(f"--value '{seen[pos]}' and '{raw}' address the same element")
            seen[pos] = raw
            positions.append(pos)

    if len(positions) == 1:
        pre_scalar = {c for c in ds.coords if ds[c].ndim == 0}
        out = ds.isel({dim: positions[0]})
        drop = [
            c
            for c in out.coords
            if out[c].ndim == 0 and c not in pre_scalar and c not in _KEEP_SCALAR_COORDS
        ]
        return out.drop_vars(drop)
    return ds.isel({dim: positions})


if __name__ == "__main__":
    select()
