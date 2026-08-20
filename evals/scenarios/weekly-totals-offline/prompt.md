# Weekly precip totals from daily rates (offline)

Workspace already contains `rates.zarr`: daily precip rates (`mm day-1`) for
15 days starting 2026-08-16 over a small East Africa grid.

Your job:

1. Aggregate to **weekly** mean rates with bins ending on **2026-08-30**
   (`aggregate-temporal --period weekly --end-time 2026-08-30`).
2. Convert those rates to period **totals** (`convert-to-totals`).
3. Write the totals Zarr to `out/weekly_totals.zarr`.

Reuse the fixture; do not fetch remote data. Prefer absolute `YYYY-MM-DD` dates.
Incomplete trailing weeks should not be scaled up to a full week.
