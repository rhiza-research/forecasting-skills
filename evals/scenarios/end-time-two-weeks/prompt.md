# Align weekly bins with --end-time

Workspace contains `rates.zarr`: 15 daily precip rate samples starting
2026-08-16.

Aggregate to weekly mean rates so the **last bin ends on 2026-08-30**, and write
`out/weekly.zarr`. You should get **two** complete weekly bins (labels ending
2026-08-23 and 2026-08-30). Drop incomplete weeks by default.

Use `aggregate-temporal --period weekly --end-time 2026-08-30`. No remote fetch.
