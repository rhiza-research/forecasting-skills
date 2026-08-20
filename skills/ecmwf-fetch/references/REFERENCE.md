# ecmwf-fetch reference

Prefer `dynamical-fetch` (`ecmwf-ifs-ens-forecast-15-day-0-25-degree` or
`ecmwf-aifs-ens-forecast`) for medium-range ECMWF. This skill is S2S via ECDS.

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

`-v` uses cfgrib short names. Instant/accumulated fields share the integer
`leadtime_hour` list above. Daily-mean fields are a separate ECDS request
whose `leadtime_hour` values are 24-hour windows aligned to those leads
(`0` → `0_24`, `168` → `144_168`, …). Pressure-level fields use
`level_type=pressure_level` and `pressure_level` (1000–10 hPa, or 1000–200
hPa for `q`). Potential vorticity uses `level_type=potential_temperature`
and level 320 K. Those vertical fields are control-forecast only.

| `-v` | ECDS `variable` | Family |
|---|---|---|
| `tp` | `total_precipitation` | instant |
| `t2m` | `2_m_temperature` | daily |
| `sst` | `sea_surface_temperature` | daily (`wtmp` accepted) |
| `d2m` | `2_m_dewpoint_temperature` | daily |
| `mx2t6` | `maximum_2_m_temperature_in_the_last_6_hours` | instant |
| `mn2t6` | `minimum_2_m_temperature_in_the_last_6_hours` | instant |
| `u10` | `10_m_u_component_of_wind` | instant |
| `v10` | `10_m_v_component_of_wind` | instant |
| `msl` | `mean_sea_level_pressure` | instant |
| `cape` | `convective_available_potential_energy` | daily |
| `tcw` | `total_column_water` | daily |
| `skt` | `skin_temperature` | daily |
| `tcc` | `total_cloud_cover` | daily |
| `sp` | `surface_pressure` | instant |
| `lsm` | `land_sea_mask` | instant |
| `orog` | `orography` | instant |
| `slt` | `soil_type` | instant |
| `sd` | `snow_depth_water_equivalent` | daily |
| `rsn` | `snow_density` | daily |
| `asn` | `snow_albedo` | daily |
| `sm20` | `soil_moisture_top_20_cm` | daily |
| `sm100` | `soil_moisture_top_100_cm` | daily |
| `st20` | `soil_temperature_top_20_cm` | daily |
| `st100` | `soil_temperature_top_100_cm` | daily |
| `ci` | `sea_ice_area_fraction` | daily |
| `sshf` | `surface_sensible_heat_flux` | instant |
| `slhf` | `surface_latent_heat_flux` | instant |
| `ssr` | `surface_net_solar_radiation` | instant |
| `ssrd` | `surface_solar_radiation_downwards` | instant |
| `str` | `surface_net_thermal_radiation` | instant |
| `strd` | `surface_thermal_radiation_downwards` | instant |
| `ttr` | `top_net_thermal_radiation` | instant |
| `cp` | `convective_precipitation` | instant |
| `sf` | `snow_fall_water_equivalent` | instant |
| `ewss` | `eastward_turbulent_surface_stress` | instant |
| `nsss` | `northward_turbulent_surface_stress` | instant |
| `ro` | `water_runoff_and_drainage` | instant |
| `sro` | `surface_runoff` | instant |
| `gh` | `geopotential_height` | pressure_level 1000–10 hPa, control only |
| `t` | `temperature` | pressure_level 1000–10 hPa, control only |
| `u` | `u_component_of_wind` | pressure_level 1000–10 hPa, control only |
| `v` | `v_component_of_wind` | pressure_level 1000–10 hPa, control only |
| `w` | `vertical_velocity` | pressure_level 1000–10 hPa, control only |
| `q` | `specific_humidity` | pressure_level 1000–200 hPa, control only |
| `pv` | `potential_vorticity` | potential_temperature 320 K, control only |
| `t20d` | `depth_of_20_C_isotherm` | daily (ocean, 1.0°) |
| `sav300` | `mean_sea_water_practical_salinity_in_the_upper_300_m` | daily (ocean, 1.0°) |
| `mswpt300` | `mean_sea_water_potential_temperature_in_the_upper_300_m` | daily (ocean, 1.0°) |
| `mlotst010` | `ocean_mixed_layer_thickness_defined_by_sigma_theta_0_01_kg_m_3` | daily (ocean, 1.0°) |
| `ocu` | `u_component_of_surface_current` | daily (ocean, 1.0°) |
| `ocv` | `v_component_of_surface_current` | daily (ocean, 1.0°) |
| `sithick` | `sea_ice_thickness` | daily (ocean, 1.0°) |
| `zos` | `sea_surface_height` | daily (ocean, 1.0°) |
| `sos` | `sea_surface_pratical_salinity` | daily (ocean, 1.0°; ECDS spelling) |

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
