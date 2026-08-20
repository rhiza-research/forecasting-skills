# ecmwf-fetch reference

## ECDS request

Both the control and perturbed retrievals target the `s2s-forecasts` collection on the ECMWF Data Store (ECDS). Shared request body:

| Key | Value |
|---|---|
| origin | `ecmwf` |
| level_type | `single_level` |
| variable | ECDS names for the requested `-v` fields (default `["total_precipitation"]`) |
| year | `[YYYY]` (from `--date`) |
| month | `[MM]` (from `--date`) |
| day | `[DD]` (from `--date`) |
| time | `["00:00"]` |
| leadtime_hour | `["0","168","240","336","480","504","672","720","840","960","1008"]` |
| forecast_type | `control_forecast` or `perturbed_forecast` |
| area | `[N, W, S, E]` |
| data_format | `grib` |

`forecast_type=perturbed_forecast` returns all 100 ensemble members in one retrieval — there is no per-member subsetting field on this collection.

`-v` uses cfgrib short names. Instant/accumulated fields (`tp`, `mx2t6`, `mn2t6`, `u10`, `v10`, `msl`) share the integer `leadtime_hour` list above. Daily-mean fields (`t2m`, `d2m`, `cape`, `tcw`) are a separate ECDS request whose `leadtime_hour` values are 24-hour windows aligned to those leads (`0` → `0_24`, `168` → `144_168`, …).

| `-v` | ECDS `variable` |
|---|---|
| `tp` | `total_precipitation` |
| `t2m` | `2_m_temperature` |
| `d2m` | `2_m_dewpoint_temperature` |
| `mx2t6` | `maximum_2_m_temperature_in_the_last_6_hours` |
| `mn2t6` | `minimum_2_m_temperature_in_the_last_6_hours` |
| `u10` | `10_m_u_component_of_wind` |
| `v10` | `10_m_v_component_of_wind` |
| `msl` | `mean_sea_level_pressure` |
| `cape` | `convective_available_potential_energy` |
| `tcw` | `total_column_water` |

## Init schedule

Real-time ECMWF S2S forecasts run **daily** at 00 UTC since IFS Cycle 48r1
(2023-06-27). Before that date, real-time inits were Mondays and Thursdays
only. (Re-forecast / hindcast calendars are separate and are not fetched by
this skill.)

## Real-time embargo

ECDS restricts access to the most recent ECMWF S2S real-time forecasts for
**2 days**. Request an init date at least 2 days before today; inits inside
the embargo window fail with a clear error suggesting an older date.

## Retrieval time

ECDS retrievals are queued and can take from a few minutes to over an hour. Bigger bboxes and the perturbed retrieval (all 100 members) queue longer than the control. The skill submits cf and pf concurrently via `client.submit()` so the overall wall time is bounded by the slower of the two.
