# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime",
#   "xarray",
# ]
# ///
"""Concatenate weather-skills envelope Zarr stores along a named dim."""

from weather_skills_core import UsageError, WroteSummary, input_path, types, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.10"


def _coerce(values):
    out = []
    for v in values:
        try:
            out.append(int(v))
        except ValueError:
            try:
                out.append(float(v))
            except ValueError:
                out.append(v)
    return out


@weather_skill(
    "concat",
    _SKILL_VERSION,
    input_type=types.ALL,
    output_type=types.ALL,
    variadic_input=True,
    extra_args={
        "dim": {"required": True},
        "coords": {"help": "Comma-separated coord values for the new dim"},
    },
)
def concat(dss, dim, coords):
    """Concatenate weather-skills envelope Zarr stores along a named dim."""
    import xarray as xr

    names = [input_path(ds).name for ds in dss]

    # Input-units guard. Concatenation places the inputs' values into a single
    # array under one set of attrs (the first input's, stamped below). If the
    # inputs hold the same variable in different units, the concatenated array
    # mixes incompatible numbers under one units label, producing objectively
    # wrong data. For each data variable common to all inputs, compare the
    # `units` attr across inputs; error when two inputs carry the variable in
    # differing units. Inputs that omit `units` for a variable are not a
    # violation (missing metadata can't be checked), so only present values
    # participate in the comparison. The check spans the union of all inputs'
    # data variables, so a variable that appears in only some inputs with
    # conflicting units is still caught; inputs lacking the variable are
    # skipped. Units are compared after stripping surrounding whitespace and
    # only when they are strings, so a trailing space is not read as a real
    # difference and a non-string attr does not break the comparison.
    all_vars = set()
    for ds in dss:
        all_vars |= set(ds.data_vars)
    for var in sorted(all_vars):
        seen_units = {}
        for name, ds in zip(names, dss, strict=True):
            if var not in ds.data_vars:
                continue
            u = ds[var].attrs.get("units")
            if not isinstance(u, str):
                continue
            seen_units[name] = u.strip()
        if len(set(seen_units.values())) > 1:
            detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
            raise UsageError(
                f"variable '{var}' has differing units across the inputs "
                f"({detail}). Concatenation combines these inputs into one array "
                f"that carries a single units label, so values measured in "
                f"different units would be mixed together as if they were the "
                f"same quantity. Concatenation requires the inputs to express "
                f"'{var}' in one consistent unit."
            )

    dim_on_inputs = all(dim in ds.dims for ds in dss)

    if not dim_on_inputs:
        if coords:
            coord_vals = _coerce([c.strip() for c in coords.split(",")])
            if len(coord_vals) != len(dss):
                raise UsageError(f"--coords len {len(coord_vals)} != inputs {len(dss)}")
            dss = [d.expand_dims({dim: [v]}) for d, v in zip(dss, coord_vals, strict=True)]
        else:
            dss = [d.expand_dims(dim) for d in dss]

    out_ds = xr.concat(dss, dim=dim)
    return out_ds, WroteSummary(f"{out_ds.sizes}", replace=True)


if __name__ == "__main__":
    concat()
