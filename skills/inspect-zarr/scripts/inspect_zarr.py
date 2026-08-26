# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy",
# ]
# ///
"""Print dimensions, coordinates, and a bounded data-variable summary of a Zarr."""

import json
import math

import numpy as np
from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

# Data arrays can be huge. Stats scan the array but the printed sample never
# exceeds this many values, even when --max-values is 0 (which still dumps
# every *coordinate* value).
_DATA_SAMPLE_CAP = 256


def _plain(da):
    return da.pint.magnitude if getattr(getattr(da, "pint", None), "units", None) else da


def _numpy(da):
    data = _plain(da)
    data = data.values if hasattr(data, "values") else data
    return np.asarray(data.compute() if hasattr(data, "compute") else data)


def _units(da):
    u = getattr(getattr(da, "pint", None), "units", None)
    if u is not None:
        return f"{u:~cf}"
    return da.attrs.get("units") or None


def _fmt(v):
    if isinstance(v, np.datetime64):
        s = str(np.datetime_as_string(v, timezone="naive"))
        return s.split("T", 1)[0] if "T00:00:00" in s else s
    v = v.item() if isinstance(v, np.generic) else v
    return v if type(v) in (int, float, str, bool) else str(v)


def _jsonable(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _show(v):
    if v is None:
        return "nan"
    if isinstance(v, float):
        return "nan" if not math.isfinite(v) else f"{v:g}"
    return str(v)


def _preview(vals, n):
    if n == 0 or len(vals) <= n:
        return list(vals), False
    h = max(1, n // 2)
    t = n - h
    if t <= 0:
        return [vals[0], "…"], True
    return [*vals[:h], "…", *vals[-t:]], True


def _stats(arr):
    flat = np.asarray(arr).reshape(-1)
    n = int(flat.size)
    out = {"n": n, "n_finite": None, "n_nan": None, "min": None, "max": None, "mean": None}
    if n == 0 or flat.dtype.kind not in "biuf":
        return out
    if flat.dtype.kind == "f":
        finite = np.isfinite(flat)
        vals = flat[finite]
        n_finite = int(vals.size)
        n_nan = n - n_finite
    else:
        vals = flat
        n_finite = n
        n_nan = 0
    out["n_finite"] = n_finite
    out["n_nan"] = n_nan
    if n_finite == 0:
        return out
    out["min"] = _jsonable(_fmt(vals.min()))
    out["max"] = _jsonable(_fmt(vals.max()))
    out["mean"] = _jsonable(float(vals.mean()))
    return out


def _data_var(name, da, max_values):
    arr = _numpy(da)
    stats = _stats(arr)
    want = _DATA_SAMPLE_CAP if max_values == 0 else min(max_values, _DATA_SAMPLE_CAP)
    flat = arr.reshape(-1)
    raw = flat[: min(want, int(flat.size))]
    sample = [_fmt(v) for v in raw]
    truncated = int(flat.size) > len(raw)
    if truncated:
        sample.append("…")
    return {
        "name": name,
        "dims": list(da.dims),
        "shape": [int(s) for s in da.shape],
        "dtype": str(arr.dtype),
        "units": _units(da),
        **stats,
        "sample": [_jsonable(v) for v in sample],
        "sample_truncated": truncated,
    }


@weather_skill(
    name="inspect-zarr",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--format", choices=["human", "json"], default="human")
@weather_skill.argument(
    "--max-values",
    type=int,
    default=24,
    help="Max values printed per coordinate and per data-variable sample (default 24). "
    "0 prints every coordinate value; data samples still cap at 256.",
)
def inspect_zarr(ds, format="human", max_values=24, **kwargs):
    """Print dimensions, coordinates, and a bounded data-variable summary of a Zarr."""
    if max_values < 0:
        raise UsageError(
            "--max-values must be >= 0 (0 prints every coordinate value; data samples stay capped)."
        )

    dims = {str(k): int(v) for k, v in ds.sizes.items()}
    coords = []
    for name, da in ds.coords.items():
        arr = _numpy(da).reshape(-1)
        values, truncated = _preview([_fmt(v) for v in arr], max_values)
        coords.append(
            {
                "name": name,
                "dims": list(da.dims),
                "dtype": str(arr.dtype),
                "units": _units(da),
                "size": int(arr.size),
                "values": values,
                "truncated": truncated,
            }
        )
    data_vars = [_data_var(name, da, max_values) for name, da in ds.data_vars.items()]

    if format == "json":
        print(json.dumps({"dims": dims, "coords": coords, "data_vars": data_vars}, indent=2))
        return

    print("Dimensions:")
    for name, size in dims.items():
        print(f"  {name}: {size}")
    print("\nCoordinates:")
    for c in coords:
        units = f" [{c['units']}]" if c["units"] else ""
        dims_s = ", ".join(c["dims"]) or "scalar"
        extra = f"  ({c['size']} values)" if c["truncated"] else ""
        shown = ", ".join(_show(v) for v in c["values"])
        print(f"  {c['name']} ({dims_s}) {c['dtype']}{units}")
        print(f"    {shown}{extra}")
    print("\nData variables:")
    for v in data_vars:
        units = f" [{v['units']}]" if v["units"] else ""
        shape = " × ".join(map(str, v["shape"])) or "scalar"
        print(f"  {v['name']} ({', '.join(v['dims'])}) {v['dtype']} {shape}{units}")
        if v["n_finite"] is not None:
            print(
                f"    finite {v['n_finite']}/{v['n']}  "
                f"min {_show(v['min'])}  max {_show(v['max'])}  mean {_show(v['mean'])}"
            )
        shown = ", ".join(_show(x) for x in v["sample"])
        extra = f"  ({len(v['sample']) - 1} of {v['n']} cells)" if v["sample_truncated"] else ""
        print(f"    sample: {shown}{extra}")


if __name__ == "__main__":
    inspect_zarr()
