# ecmwf-fetch reference

## MARS request

Both the control (`cf`) and perturbed (`pf`) retrievals use:

| Key | Value |
|---|---|
| class | s2 |
| dataset | s2s |
| expver | prod |
| levtype | sfc |
| model | glob |
| origin | ecmf |
| param | 228228 (total precipitation, m) |
| step | 0/168/240/336/480/504/672/720/840/960/1008 |
| stream | enfo |
| time | 00:00:00 |
| area | N/W/S/E bbox |
| type | `cf` or `pf` |

`pf` additionally sets `number: 1/to/100`.

## Named regions

| Region | Bbox N/W/S/E |
|---|---|
| africa | 23/-20/-37/59 |
| kenya | 7/32/-6/43 |
| ghana | 12/-4/4/2 |
| senegal | 17/-17.5/12/-11 |
| ethiopia | 16/32/2/49 |

## Retrieval time

MARS retrievals are asynchronous and can take from a few minutes to over an hour. Bigger bboxes and more ensemble members queue longer. Control-only retrievals are an order of magnitude faster than perturbed ensemble retrievals.
