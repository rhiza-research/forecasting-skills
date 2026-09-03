# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime>=1.6",
#   "numpy>=2.4",
#   "xarray>=2026.4",
#   "pint-xarray>=0.6",
# ]
# ///
"""Standardized anomaly: (ds[var] - clim[var_avg]) / clim[var_std]."""

import pint_xarray

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"


@weather_skill(
    name="standardize-anomaly",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--climatology", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append", required=True)
@weather_skill.argument("--epsilon", type=float, default=0.1, help="Normalization to avoid divide-by-zero.")
def standardize_anomaly(ds, climatology, variable, epsilon, **kwargs):
    """Standardized anomaly: (ds[var] - clim[var_avg]) / clim[var_std] per --variable."""
    import xarray as xr

    # Harmonize names and values on spatial dims.
    # TODO: this should be standardized in some core function.
    try:
        ds_lat, ds_lon = detect_spatial_dims(ds)
        clim_lat, clim_lon = detect_spatial_dims(climatology)
    except UsageError:
        ds_lat = clim_lat = None

    if ds_lat and clim_lat:
        for src_name, other_name, target in (
            (ds_lat, clim_lat, "latitude"),
            (ds_lon, clim_lon, "longitude"),
        ):
            ds = ds.rename({src_name: target})
            climatology = climatology.rename({other_name: target})
            ds = ds.assign_coords({target: ds[target].astype("float64").round(4)})
            climatology = climatology.assign_coords(
                {target: climatology[target].astype("float64").round(4)}
            )

    vars_ = list(dict.fromkeys(variable))
    missing_input = [v for v in vars_ if v not in ds.data_vars]
    missing_clim = [
        f"{v}_{stat}" for v in vars_ for stat in ("avg", "std") if f"{v}_{stat}" not in climatology.data_vars
    ]
    if missing_input or missing_clim:
        parts = []
        if missing_input:
            parts.append(f"--input missing {missing_input} (have {list(ds.data_vars)})")
        if missing_clim:
            parts.append(f"--climatology missing {missing_clim} (have {list(climatology.data_vars)})")
        raise UsageError("; ".join(parts))

    out = {}
    for v in vars_:
        avg_name, std_name = f"{v}_avg", f"{v}_std"
        field = ds[v]
        avg, std = climatology[avg_name], climatology[std_name]

        # epsilon regularizes the denominator (in std's own units) so a
        # near-zero-variance cell doesn't blow up to inf. An offset unit
        # (e.g. degree_Celsius) can't add a plain quantity in that unit —
        # add it as a delta instead, which is what a magnitude bump means.
        std_units = std.pint.units if getattr(std, "pint", None) is not None else None
        if std_units is None:
            denom = std + epsilon
        else:
            try:
                denom = std + epsilon * std_units
            except pint_xarray.pint.OffsetUnitCalculusError:
                denom = std + std.pint.registry.Quantity(epsilon, f"delta_{std_units}")

        # Pint checks unit compatibility itself and auto-converts compatible
        # units; unlike `difference`, we refuse outright on a real mismatch.
        try:
            anomaly = (field - avg) / denom
        except (pint_xarray.pint.DimensionalityError, pint_xarray.errors.PintExceptionGroup) as exc:
            raise UsageError(
                f"cannot standardize {v!r} against {avg_name!r}/{std_name!r}: {exc}. "
                "standardize-anomaly refuses to combine dimensionally incompatible "
                "units. Run unit-convert first."
            ) from None

        if getattr(anomaly, "pint", None) is not None and anomaly.pint.units is not None:
            anomaly = anomaly.pint.dequantify()
        anomaly.attrs = {"units": "1", "long_name": f"{v} standardized anomaly"}
        out[f"{v}_anomaly"] = anomaly

    return xr.Dataset(out)


if __name__ == "__main__":
    standardize_anomaly()
