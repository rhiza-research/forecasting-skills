# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
# ]
# ///
"""Subtract one weather-skills envelope Zarr from another (A - B).

Takes exactly two inputs: the first is the minuend (A), the second the
subtrahend (B). Subtraction is xarray-aligned (inner join on shared dims) with
broadcasting over dims present on only one side, so a ``(time, latitude,
longitude)`` field minus a ``(latitude, longitude)`` baseline (e.g. a
time-mean from ``reduce``) yields per-time anomalies. The output keeps the
first input's attrs.
"""

import sys

from weather_skills_core import UsageError, WroteSummary, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.6"


def _to_signed(da, np):
    """Promote a boolean or unsigned-integer DataArray to a type that can
    hold a negative difference, leaving signed-int / float / other dtypes
    untouched. Boolean becomes int16; an unsigned int becomes the next signed
    int wide enough to represent its negatives (uint8->int16, uint16->int32,
    uint32->int64, uint64->int64 — the int64 cast does not clamp, so a uint64
    value above int64 max overflows/wraps), so A - B does not return a
    wrapped-around modulo-2**nbits value."""
    kind = da.dtype.kind
    if kind == "b":
        return da.astype(np.int16)
    if kind == "u":
        widen = {1: np.int16, 2: np.int32, 4: np.int64, 8: np.int64}
        return da.astype(widen.get(da.dtype.itemsize, np.int64))
    return da


def _normalize_args(args):
    # Normalize provenance args before stamping so reordered or duplicated
    # --variable flags don't cause spurious cache misses: dedupe and sort.
    if args.get("variable") is not None:
        args["variable"] = sorted(set(args["variable"]))
    return args


@weather_skill(
    "difference",
    _SKILL_VERSION,
    input_type=["any", "any"],
    output_type="same",
    input_help="Input Zarr; pass exactly twice (first = A, the minuend; second = B, the subtrahend)",
    input_paths=True,
    variable={
        "mode": "repeat",
        "help": "Data variable to difference. Repeat once per variable to select "
        "several; each must be a data variable of BOTH inputs. Default (unset) "
        "differences every data variable present in both inputs.",
    },
    normalize_args=_normalize_args,
)
def difference(ds_a, ds_b, input_paths, variable):
    """Subtract one weather-skills envelope Zarr from another (A - B)."""
    import numpy as np
    import xarray as xr

    names = [p.name for p in input_paths]

    # Variable selection. Explicit --variable names must be data variables of
    # BOTH inputs. Default selection takes every data variable present in
    # both (in the first input's order); the inputs sharing none is an error.
    # Data variables present in only one input are not differenced and are
    # dropped from the output.
    shared = [v for v in ds_a.data_vars if v in ds_b.data_vars]
    if variable is not None:
        # De-duplicate while preserving first-seen order so a repeated name
        # doesn't difference a variable twice.
        selected = list(dict.fromkeys(variable))
        for var in selected:
            absent = [
                name
                for name, ds in zip(names, (ds_a, ds_b), strict=True)
                if var not in ds.data_vars
            ]
            if absent:
                raise UsageError(
                    f"--variable '{var}' is not a data variable of {absent}. "
                    f"{names[0]} has {list(ds_a.data_vars)}; "
                    f"{names[1]} has {list(ds_b.data_vars)}."
                )
    else:
        if not shared:
            raise UsageError(
                f"the inputs share no data variables "
                f"({names[0]} has {list(ds_a.data_vars)}; "
                f"{names[1]} has {list(ds_b.data_vars)})."
            )
        selected = shared
    dropped = sorted({v for ds in (ds_a, ds_b) for v in ds.data_vars if v not in selected})
    if dropped:
        print(
            f"Note: dropping data variable(s) {dropped} not differenced "
            f"(absent from an input or unselected).",
            file=sys.stderr,
        )

    # Input-units check. The output variable keeps the first input's attrs,
    # including its `units`. If the inputs hold the variable in different
    # units, the subtraction operates on raw values in incompatible scales,
    # so warn and proceed (run unit-convert first to put both inputs on one
    # units basis). Compare only string `units` attrs, stripped of
    # surrounding whitespace so a trailing space is not read as a real
    # difference.
    for var in selected:
        seen_units = {}
        for name, ds in zip(names, (ds_a, ds_b), strict=True):
            u = ds[var].attrs.get("units")
            if isinstance(u, str):
                seen_units[name] = u.strip()
        if len(set(seen_units.values())) > 1:
            detail = ", ".join(f"{name} units={u!r}" for name, u in seen_units.items())
            print(
                f"Warning: variable '{var}' has differing units across the "
                f"inputs ({detail}). The subtraction operates on the raw "
                f"values, so the result mixes incompatible scales; convert "
                f"the inputs onto one units basis with unit-convert first. "
                f"The output keeps the first input's units.",
                file=sys.stderr,
            )

    # Shared dims WITHOUT an index coordinate can't be label-aligned: xarray
    # pairs them positionally by integer position. A size mismatch there can't
    # be reconciled (the subtraction would raise an opaque broadcast error),
    # so exit cleanly naming the dim; on equal sizes warn that the pairing is
    # positional (element i of A minus element i of B), since a coordinateless
    # dim carries no labels to confirm the rows actually correspond.
    shared_dims = set(ds_a.dims) & set(ds_b.dims)
    for d in sorted(shared_dims):
        if d in ds_a.indexes and d in ds_b.indexes:
            continue  # indexed on BOTH sides => label-aligned, not positional
        # A dim indexed on only one side cannot be label-aligned either: xarray
        # has no labels on the other side to join against. Fall through to the
        # positional size check so a mismatch exits cleanly here rather than
        # surfacing an opaque broadcast/alignment error downstream.
        size_a = ds_a.sizes[d]
        size_b = ds_b.sizes[d]
        if size_a != size_b:
            raise UsageError(
                f"shared dim '{d}' has no index coordinate, so it is "
                f"paired positionally, but the inputs disagree on its size "
                f"({names[0]}={size_a}, {names[1]}={size_b}); "
                "there is no way to align unlabeled rows of different length."
            )
        print(
            f"Warning: shared dim '{d}' has no index coordinate; pairing it "
            f"positionally (element i of {names[0]} minus element i of "
            f"{names[1]}). Verify the rows correspond.",
            file=sys.stderr,
        )

    print(
        f"Differencing {names[0]} - {names[1]} variables={selected}",
        file=sys.stderr,
    )

    # A - B per variable. xarray arithmetic inner-joins the shared dims and
    # broadcasts over dims present on only one side, so a (time, latitude,
    # longitude) field minus a (latitude, longitude) baseline yields
    # per-time anomalies. Arithmetic drops attrs; restore the first input's.
    data_vars = {}
    for var in selected:
        da_a = ds_a[var]
        da_b = ds_b[var]

        # Cast boolean and unsigned-integer operands to a signed/float type
        # before subtracting: bool subtraction is nonsensical (and numpy
        # deprecates it), and unsigned subtraction wraps around modulo
        # 2**nbits, turning a small negative result into a huge positive one.
        # Promote bool -> int16 and unsigned int -> the next signed int wide
        # enough to hold negatives (falling back to int64).
        da_a = _to_signed(da_a, np)
        da_b = _to_signed(da_b, np)

        # An input dim that is already empty before alignment is distinct from
        # an alignment that produced no overlap; report which.
        pre_empty = [
            d
            for d in set(da_a.sizes) | set(da_b.sizes)
            if da_a.sizes.get(d, 1) == 0 or da_b.sizes.get(d, 1) == 0
        ]
        diff = da_a - da_b
        empty = [d for d, s in diff.sizes.items() if s == 0]
        if empty:
            if set(empty) & set(pre_empty):
                raise UsageError(
                    f"variable '{var}' is already empty along dim(s) "
                    f"{sorted(set(empty) & set(pre_empty))} in an input before "
                    "alignment: there is nothing to subtract."
                )
            raise UsageError(
                f"aligning the inputs left variable '{var}' empty "
                f"along dim(s) {empty}: the inputs have no overlapping "
                "coordinate values there, so there is nothing to subtract."
            )
        diff.attrs = dict(ds_a[var].attrs)
        data_vars[var] = diff
    out_ds = xr.Dataset(data_vars)
    return out_ds, WroteSummary(f"{out_ds.sizes}", replace=True)


if __name__ == "__main__":
    difference()
