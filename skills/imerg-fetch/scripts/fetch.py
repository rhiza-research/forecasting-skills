# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
#   "cftime",
#   "earthaccess",
#   "h5netcdf",
#   "h5py",
#   "dask",
#   "xarray",
#   "zarr",
#   "numpy",
#   "pint-xarray>=0.6",
# ]
# ///
"""Fetch IMERG live precipitation and write a weather-skills standard dataset Zarr."""

import sys
import tempfile
from datetime import timedelta

from pathlib import Path

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.cf import stamp_cf_attrs
from weather_skills_core.units import to_standard_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.14"

SHORTNAMES = {
    "late": "GPM_3IMERGDL",
    "final": "GPM_3IMERGDF",
}


@weather_skill(
    name="imerg-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument("--version", default="late", choices=list(SHORTNAMES))
def fetch(start_time, end_time, version, **kwargs):
    """Fetch IMERG live precipitation and write a weather-skills standard dataset Zarr."""
    import earthaccess
    import numpy as np
    import xarray as xr

    shortname = SHORTNAMES[version]
    start = start_time.isoformat()
    end = end_time.isoformat()

    print(f"Fetching IMERG {version} ({shortname}) {start} -> {end}", file=sys.stderr)

    earthaccess.login()
    results = earthaccess.search_data(
        short_name=shortname,
        cloud_hosted=True,
        temporal=(start, end),
    )
    if not results:
        raise RuntimeError(f"No IMERG {version} granules found in {start}..{end}")
    print(f"Found {len(results)} granules", file=sys.stderr)

    requested_span = (end_time - start_time).days + 1

    with tempfile.TemporaryDirectory(prefix="imerg-fetch-") as td:
        files = earthaccess.download(results, local_path=td)
        ds = xr.open_mfdataset(files, engine="h5netcdf", combine="by_coords")
        ds = ds[["precipitation"]].rename({"precipitation": "precip"})
        spatial_rename = {
            src: dst
            for src, dst in {"lat": "latitude", "lon": "longitude"}.items()
            if src in ds.dims
        }
        if spatial_rename:
            ds = ds.rename(spatial_rename)
        ds = ds.sel(time=slice(start, end))

        present_days = sorted({np.datetime64(t, "D").item() for t in ds["time"].values})
        present_days = [d for d in present_days if start_time <= d <= end_time]
        covered_days = len(present_days)
        if covered_days == 0:
            raise UsageError(f"no granule day falls within {start}..{end}")
        if covered_days < requested_span:
            last_present = present_days[-1]
            expected_tail = [
                start_time + timedelta(days=i)
                for i in range(requested_span)
                if start_time + timedelta(days=i) > last_present
            ]
            missing_days = sorted(
                {start_time + timedelta(days=i) for i in range(requested_span)} - set(present_days)
            )
            if missing_days != expected_tail:
                raise UsageError(
                    f"non-trailing missing day(s) "
                    f"{', '.join(d.isoformat() for d in missing_days)} within "
                    f"{start}..{end} — server/data gap, not lag. Refusing to write "
                    "a partial zarr with a hole in the middle."
                )
            print(
                f"WARNING: requested {requested_span} days ({start}..{end}) but only "
                f"{covered_days} distinct day(s) are present in that span; writing the "
                f"available days through {last_present.isoformat()} (trailing days not yet "
                "published, or near the dataset start).",
                file=sys.stderr,
            )

        ds = ds.load()
        ds = ds.drop_attrs()
        ds.attrs.update(Conventions="CF-1.13", weather_skills_source="imerg")
        ds["precip"].attrs.update(
            units="mm day-1",
            standard_name="lwe_precipitation_rate",
            long_name="IMERG daily precipitation",
        )
        stamp_cf_attrs(ds)
        return to_standard_units(ds, variables=["precip"])


if __name__ == "__main__":
    fetch()
