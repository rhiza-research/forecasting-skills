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

## Named regions

| Region | Bbox N/W/S/E |
|---|---|
| africa | 23/-20/-37/59 |
| kenya | 7/32/-6/43 |
| ghana | 12/-4/4/2 |
| senegal | 17/-17.5/12/-11 |
| ethiopia | 16/32/2/49 |
| namibia | -15/10/-31/27 |
| botswana | -15/18/-28/31 |
| zambia | -6/20/-20/35 |
| madagascar | -10/42/-27/52 |
| angola | -5/12/-18/24 |

## Retrieval time

ECDS retrievals are queued and can take from a few minutes to over an hour. Bigger bboxes and the perturbed retrieval (all 100 members) queue longer than the control. The skill submits cf and pf concurrently via `client.submit()` so the overall wall time is bounded by the slower of the two.
