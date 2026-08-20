# Deaccumulate before aggregating a cumulative forecast

Workspace contains `forecast_tp.zarr`: an ECMWF-style forecast with cumulative
total precipitation (`tp`, amount units) on a `step` axis.

Produce weekly **mean rates** on the step axis:

1. Convert the cumulative field to per-step rates with `deaccumulate`.
2. Aggregate with `aggregate-temporal --period weekly`.
3. Write the result to `out/weekly_rates.zarr`.

Do **not** run `aggregate-temporal` on the still-cumulative field. Do not fetch
remote data.
