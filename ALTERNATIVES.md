# Existing tools surveyed

This doc captures the landscape survey for CLI tools that overlap with the
generic middle-pipeline skills in `skills/` (`clip-region`,
`aggregate-temporal`, `downscale`, `concat`, `plot`, `plot-compare`).
Recorded for future reference so we don't re-survey from scratch when
scaling up or deciding what to swap in.

Decision at time of survey: keep our current xarray-based wrappers. They
handle the shapes we care about (forecast ensembles with `number`/`step`,
station envelopes) and the data sizes we care about (MB–low-GB per
country/date). The alternatives below become worth it when data size,
access patterns, or production ops scale past that.

## Directly relevant

### CDO — Climate Data Operators (C)
- <https://code.mpimet.mpg.de/projects/cdo>
- Gold-standard CLI for climate data. Hundreds of operators: clip, aggregate,
  regrid, statistics, arithmetic.
- **Does not support Zarr natively.** GRIB/netCDF in, GRIB/netCDF out. A
  Zarr → netCDF → CDO → netCDF → Zarr roundtrip works but is slow and loses
  Zarr's chunking benefits.
- When to reach for it: when the operation is already in CDO and the data is
  in netCDF (or small enough that roundtripping is cheap).

### GDAL (C)
- <https://gdal.org/en/latest/drivers/raster/zarr.html>
- Zarr driver supports v2 and v3, read and write.
- `gdalwarp -te W S E N -of Zarr` clips. `gdalwarp -tr dx dy -r average` coarsens.
  `gdal_translate` subsets / converts.
- **Raster model**: treats Zarr as (band, y, x). ECMWF ensemble forecast
  `(number, step, latitude, longitude)` doesn't pass through cleanly — dims
  beyond bands get flattened.
- When to reach for it: 2D or 3D spatial ops on raster-shaped Zarr. Especially
  reprojection (where xarray alternatives are weak).

### xcube (Python)
- <https://xcube.readthedocs.io/en/latest/overview.html>
- Production EO toolkit from Brockmann/ESA. CLI and library. Zarr-native via
  xarray + dask.
- `xcube resample` — temporal up/down sampling with `--frequency` incl. `all`
  for full-dataset reduction.
- `xcube gen`, subset, aggregate, concat commands.
- When to reach for it: this is the closest semantic match to our generic
  middle skills. For TB-scale workloads or production deployments it is the
  real drop-in. Keeping our lighter wrappers while this exists is a scale
  decision, not a capability decision.

### xclim (Python)
- <https://xclim.readthedocs.io/en/latest/notebooks/cli.html>
- CLI for computing climate indicators (frost days, heating degree days, etc.)
  on netCDF.
- Narrow purpose — indicator calculation, not generic slicing/aggregation.
- Relevant if we ever add an `indicators` skill.

### icclim (Python)
- <https://pypi.org/project/icclim/>
- ECA&D / ETCCDI climate indices. Adjacent to xclim; same domain.

## Zarr-format tooling (format-level, not scientific ops)

### zarr-python CLI
- <https://zarr.readthedocs.io/en/stable/user-guide/cli/>
- `zarr migrate v3`, `zarr remove-metadata`, proposed `cp`/`rm`/`convert`.
- Store-level operations only. No scientific ops.

### zarrs_tools (Rust)
- <https://github.com/zarrs/zarrs_tools>
- `zarrs_reencode` (chunks/codecs), `zarrs_filter` (crop, downsample, rescale,
  gaussian, gradient magnitude, noise), `zarrs_ome`, `zarrs_info`,
  `zarrs_validate`, `zarrs_binary2zarr`.
- Array-level (no xarray/CF semantics) — clipping by coord value isn't
  possible, only by index. Fast if you're already in index-space.

### rechunker (Python)
- <https://rechunker.readthedocs.io/>
- Single purpose: rechunk a Zarr store for optimal downstream access.
- Worth pulling in if we ever want cloud-optimized outputs. Currently our
  fetchers use `to_zarr()` defaults; fine at our scale.

### zarrdump
- <https://discourse.pangeo.io/t/zarrdump-printing-metadata-of-zarrs-from-the-command-line/1066>
- Dumps Zarr metadata. Debugging aid, not a pipeline tool.

### ngff-zarr (Python)
- <https://ngff-zarr.readthedocs.io/en/latest/cli.html>
- OME-NGFF focused (microscopy). Wrong domain for weather/climate.

### zarrify (opendatacube)
- <https://github.com/opendatacube/datacube-zarr/blob/master/docs/zarrify.md>
- Raster → Zarr ingress conversion. Ingress only.

### climate-zarr (PyPI)
- <https://pypi.org/project/climate-zarr/>
- netCDF→Zarr + US-county wizard. Closest single-package match to our goals
  but US-scoped and built around an interactive wizard.

## When to revisit

Swap a skill for one of the above when:
- **Scale**: the skill is hitting memory/time limits on real inputs (>10 GB per
  call, or >1 minute per call for what used to be sub-second).
- **Missing capability**: we need an operation our wrapper can't do cleanly —
  most likely reprojection (→ GDAL) or statistical downscaling (→ xsd /
  scikit-downscale).
- **Production deployment**: the pipeline is running on a shared cluster or
  cloud job; `xcube` + dask is the natural scaling path.

Until then, the xarray-based wrappers in `skills/` are the right trade: one
implementation, xarray-native semantics for forecast/ensemble/station dims,
minimal external deps.
