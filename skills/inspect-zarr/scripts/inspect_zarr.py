# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime>=1.6",
#   "numpy",
# ]
# ///
"""Print dimensions, coordinate values, and data-variable summary of a Zarr."""

import json

import numpy as np
from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


def _numpy(da):
    data = da.pint.magnitude if getattr(getattr(da, "pint", None), "units", None) else da.values
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


def _preview(vals, n):
    if n == 0 or len(vals) <= n:
        return list(vals), False
    h = max(1, n // 2)
    t = n - h
    if t <= 0:
        return [vals[0], "…"], True
    return [*vals[:h], "…", *vals[-t:]], True


@weather_skill(
    name="inspect-zarr",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--format", choices=["human", "json"], default="human")
@weather_skill.argument("--max-values", type=int, default=24)
def inspect_zarr(ds, format="human", max_values=24, **kwargs):
    """Print dimensions, coordinate values, and data-variable summary of a Zarr."""
    if max_values < 0:
        raise UsageError("--max-values must be >= 0 (0 prints every coordinate value).")

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
    data_vars = [
        {
            "name": name,
            "dims": list(da.dims),
            "shape": [int(s) for s in da.shape],
            "dtype": str(_numpy(da).dtype),
            "units": _units(da),
        }
        for name, da in ds.data_vars.items()
    ]

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
        shown = ", ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in c["values"])
        print(f"  {c['name']} ({dims_s}) {c['dtype']}{units}")
        print(f"    {shown}{extra}")
    print("\nData variables:")
    for v in data_vars:
        units = f" [{v['units']}]" if v["units"] else ""
        shape = " × ".join(map(str, v["shape"])) or "scalar"
        print(f"  {v['name']} ({', '.join(v['dims'])}) {v['dtype']} {shape}{units}")


if __name__ == "__main__":
    inspect_zarr()
