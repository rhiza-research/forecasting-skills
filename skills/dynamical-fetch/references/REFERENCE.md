# dynamical-fetch reference

Default forecast/analysis fetcher when the catalog has the product. Use
`ecmwf-fetch` only for ECMWF S2S. Default IMERG source:
`nasa-imerg-analysis-late` / `nasa-imerg-analysis-early` — do not start
with `imerg-fetch`.

## Library

Datasets are opened with [`dynamical-catalog`](https://github.com/dynamical-org/dynamical-catalog):

```python
import dynamical_catalog

dynamical_catalog.list()  # -> list of dataset id strings
ds = dynamical_catalog.open(id)  # -> lazy, icechunk-backed xarray.Dataset
```

`open()` reads only metadata until values are computed, so shape detection,
`latest` resolution (max `init_time`/`time`), the cache check, and bbox/time/
variable subsetting all happen before any array bytes are pulled.

## Catalog datasets and native dimensions

`dynamical_catalog.list()` returns the available dataset ids. Those on regular
1-D lat/lon grids are handled by this skill; the HRRR datasets are on a projected
grid and are rejected (see below).

| Dataset id | Shape | Native dims | Members |
|---|---|---|---|
| `noaa-gefs-forecast-35-day` | ensemble forecast | `init_time, ensemble_member, lead_time, latitude, longitude` | 31 |
| `ecmwf-ifs-ens-forecast-15-day-0-25-degree` | ensemble forecast | `init_time, lead_time, ensemble_member, latitude, longitude` | 51 |
| `ecmwf-aifs-ens-forecast` | ensemble forecast | `init_time, lead_time, ensemble_member, latitude, longitude` | 51 |
| `noaa-gfs-forecast` | deterministic forecast | `init_time, lead_time, latitude, longitude` | — |
| `ecmwf-aifs-single-forecast` | deterministic forecast | `init_time, lead_time, latitude, longitude` | — |
| `dwd-icon-eu-forecast-5-day` | deterministic forecast | `init_time, lead_time, latitude, longitude` | — |
| `noaa-gfs-analysis` | analysis | `time, latitude, longitude` | — |
| `noaa-gefs-analysis` | analysis | `time, latitude, longitude` | — |
| `noaa-mrms-conus-analysis-hourly` | analysis | `time, latitude, longitude` | — |
| `nasa-imerg-analysis-early` | analysis | `time, latitude, longitude` | — |
| `nasa-imerg-analysis-late` | analysis | `time, latitude, longitude` | — |
| `noaa-hrrr-forecast-48-hour` | **rejected** — projected | `init_time, lead_time, y, x` (2-D lat/lon) | — |
| `noaa-hrrr-analysis` | **rejected** — projected | `time, y, x` (2-D lat/lon) | — |

Shape is detected from the dims present, not from this table: `ensemble_member`
→ ensemble forecast, else `lead_time` → deterministic forecast, else `time` →
analysis.

## Coordinate conventions (verified on the catalog stores)

- `latitude`: 1-D, **descending** on these GRIB-derived stores (e.g. 90 → −90 at
  0.25°). `longitude`: 1-D, **ascending**, −180 … 179.75. The bbox slice keys
  off each axis's own order, so it is correct either way.
- `init_time`: `datetime64`. `lead_time`: `timedelta64`. `ensemble_member`:
  `int16`, **0-indexed with member 0 as the control** — already the standard dataset's
  `number` convention.
- Bookkeeping coords present on forecast stores — `valid_time`,
  `expected_forecast_length`, `ingested_forecast_length` — and the CRS scalar
  `spatial_ref` are dropped on output (not part of the standard dataset).

## dynamical → weather-skills standard dataset mapping

| dynamical | Envelope | Notes |
|---|---|---|
| `init_time` (selected to one date) | scalar `time` coord | the 00 UTC init of the resolved `--date` |
| `lead_time` | `step` dim | renamed; stays `timedelta64` |
| `ensemble_member` | `number` dim | renamed; member 0 = control |
| `latitude` / `longitude` | `latitude` / `longitude` | unchanged (1-D) |
| `time` (analysis) | `time` dim | sliced to `--start-time`/`--end-time`, kept |
| `*_Nhpa` data variables | prefix + `vertical` dim | stacked; `vertical` is pressure in hPa (`positive=down`). Height-above-ground fields (`temperature_2m`, `wind_u_80m`) are not stacked. |
| other data variables | data variables | known precip → `mm day-1`; known air temp → `degree_Celsius` |

Forecast `--date` selects the **00 UTC** initialization of the resolved date
(`init_time == <date>T00:00:00`); all supported forecast datasets publish a 00
UTC cycle. A date with no matching init exits 1 and prints the available init
range.

The catalog does not store a native vertical axis. Pressure-level fields are
separate 2-D variables, stacked here onto `vertical`:

| Dataset | Pressure-level fields |
|---|---|
| `ecmwf-ifs-ens-forecast-15-day-0-25-degree`, `ecmwf-aifs-ens-forecast`, `ecmwf-aifs-single-forecast` | `temperature_{850,925}hpa`, `geopotential_height_{500,850,925}hpa` |
| `noaa-gefs-forecast-35-day`, `noaa-gefs-analysis` | `geopotential_height_500hpa` only |
| `noaa-gfs-forecast`, `noaa-gfs-analysis`, `dwd-icon-eu-forecast-5-day`, IMERG, MRMS | none |

`-v t` / `-v gh` (or the prefixes `temperature` / `geopotential_height`) expand
to every `*_Nhpa` field of that prefix. `temperature_2m` is not included.

## Projected (HRRR) grids

The HRRR stores use a Lambert Conformal Conic projection: native dims are 1-D
`y`/`x` in **meters** (3000 m spacing), with `latitude` and `longitude` stored
as 2-D `(y, x)` fields and the CRS in `spatial_ref`
(`grid_mapping_name=lambert_conformal_conic`, central meridian −97.5°). There is
no 1-D latitude axis to slice. A faithful bbox subset stays curvilinear (`y`/`x`
+ 2-D lat/lon), which the 1-D-lat/lon standard dataset does not model. Producing a
regular lat/lon standard dataset from such a grid requires reprojection with
interpolation — a grid transform — so it is out of scope for this fetcher, which
rejects the two HRRR ids. The skill detects this generically: any opened dataset
without 1-D `latitude` and `longitude` dims is rejected.
