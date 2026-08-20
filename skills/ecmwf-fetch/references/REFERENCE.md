# ecmwf-fetch reference

## ECDS request

Both the control and perturbed retrievals target the `s2s-forecasts` collection on the ECMWF Data Store (ECDS). Shared request body:

| Key | Value |
|---|---|
| origin | `ecmwf` |
| level_type | `single_level` |
| variable | `["total_precipitation"]` |
| year | `[YYYY]` (from `--date`) |
| month | `[MM]` (from `--date`) |
| day | `[DD]` (from `--date`) |
| time | `["00:00"]` |
| leadtime_hour | `["0","168","240","336","480","504","672","720","840","960","1008"]` |
| forecast_type | `control_forecast` or `perturbed_forecast` |
| area | `[N, W, S, E]` |
| data_format | `grib` |

`forecast_type=perturbed_forecast` returns all 100 ensemble members in one retrieval — there is no per-member subsetting field on this collection.

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
