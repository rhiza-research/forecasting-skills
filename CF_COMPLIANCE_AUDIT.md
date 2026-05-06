# CF-compliance audit

Per-skill audit of `forecasting-skills/skills/*` against the envelope's CF
expectations: spatial/time coords carry CF `standard_name`, `units`, `axis`;
data variables carry `units` and (where applicable) `standard_name`; consumers
identify coords via CF metadata (with cf-xarray name fallback) so they tolerate
variable-name / unit / coord-name variation in inputs.

The contract being audited is the one stated in `ENVELOPE.md` ("Conventions"
section) and the README's "Composition pattern" / "Envelope contract" claims.

## How each skill was scored

Each skill is scored on three axes:

- **In**: when reading a Zarr, does the skill identify coords / variables in a
  way that survives reasonable input variation (CF metadata or sensible name
  fallback)? `n/a` if the skill does not read a Zarr.
- **Out**: when writing a Zarr, does the skill stamp CF metadata on coords and
  data variables, and clear `.encoding` per the envelope contract? `n/a` if
  the skill does not write a Zarr.
- **Variation**: does the skill tolerate non-canonical inputs without a code
  change — alternate dim names (`lat/lon/y/x` vs `latitude/longitude`),
  alternate variable names, or units that differ from a hard-coded default?

`pass` = the skill meets the axis as designed. `fail` = a concrete gap that
breaks an envelope claim. `note` = passes but with a caveat worth recording.

## Summary

| Skill              | In   | Out  | Variation | Overall |
|---|---|---|---|---|
| `ecmwf-fetch`      | n/a  | note | n/a       | pass    |
| `chirps-fetch`     | n/a  | pass | n/a       | pass    |
| `imerg-fetch`      | n/a  | fail | n/a       | **fail** |
| `tahmo-fetch`      | n/a  | fail | n/a       | **fail** |
| `clip-region`      | pass | pass | pass      | pass    |
| `aggregate-temporal` | pass | fail | pass    | **fail** |
| `downscale`        | pass | pass | pass      | pass    |
| `concat`           | pass | pass | pass      | pass    |
| `plot`             | pass | n/a  | pass      | pass    |
| `plot-compare`     | note | n/a  | pass      | pass    |
| `plot-mediogram`   | pass | n/a  | note      | pass    |
| `email-report`     | n/a  | n/a  | n/a       | n/a     |

3 fail, 1 n/a, 8 pass (2 with notes).

## Per-skill detail

### `ecmwf-fetch` — pass (note on data-var attrs)

`scripts/fetch.py:48–65` (`_stamp_cf_attrs`) walks `(latitude, lat, y)` and
`(longitude, lon, x)` and stamps `standard_name`/`units`/`axis` on the first
match; stamps `time` similarly. `step` is left as bare timedelta64 (CF has no
direct timedelta concept, and the envelope spec explicitly only requires the CF
T-axis on `time`).

`for v in ds.variables: ds[v].encoding = {}` (line 129–130) clears the codec
encoding per the envelope rule. `rhiza_*` root attrs are stamped.

**Note**: data-variable CF metadata (`standard_name`, `units` on `tp`) is
inherited from the cfgrib decode of the GRIB tables and not re-asserted by
this script. That is the standard cfgrib behavior and is normally correct, but
if a future GRIB request changes the param table, the script will not catch
it. Not blocking; recorded as a downstream brittleness.

### `chirps-fetch` — pass

`scripts/fetch.py:58–70` stamps coord CF attrs on `lat`/`lon`/`time`. Lines
100–103 stamp `units = "mm/day"`, `standard_name =
"lwe_thickness_of_precipitation_amount"`, and `long_name` on the `precip` data
variable. `.encoding = {}` cleared (line 109–110). `rhiza_source` and
`rhiza_date` set. Fully compliant.

### `imerg-fetch` — **fail** (data-variable CF metadata)

`scripts/fetch.py:93` calls `ds = ds.drop_attrs()` after the rename, which
strips both root and per-variable attrs. The script then re-applies coord CF
attrs via `_stamp_cf_attrs` and root `rhiza_*` attrs (lines 94–95) but **never
re-stamps `units` or `standard_name` on the `precip` data variable**. After
the rename of `precipitation` → `precip`, the resulting Zarr has a `precip`
variable with no CF metadata at all.

That breaks the envelope's "Data variable units should follow CF where
possible" claim and means consumers (e.g. `plot`, which reads
`da.attrs.get("units")` for the colorbar) get an empty unit label when the
input is IMERG.

Fix sketch: after `drop_attrs()`, set `ds["precip"].attrs.update(units="mm/hr",
standard_name="lwe_precipitation_rate", long_name="IMERG precipitation rate")`
(units pending verification of the specific shortname — `GPM_3IMERGDL` is
mm/hr per the IMERG L3 product spec; confirm before stamping).

### `tahmo-fetch` — **fail** (data-variable CF metadata)

`scripts/fetch.py:196–214` stamps CF attrs on `latitude`, `longitude`, `time`,
`station_id` (with `cf_role = "timeseries_id"`, the CF DSG convention) and
sets `featureType = "timeSeries"` on the root. That is correct for the station
envelope.

But the actual data variables (`precip`, `temperature`, `humidity`,
`pressure`) **never get `units` or `standard_name`**. The aggregations are
done in pandas (sum / mean) on raw TAHMO values whose units are mm,
°C, %, hPa — none of which are recorded on the resulting xarray variables.
This is the same envelope violation as `imerg-fetch`.

Fix sketch: after `xr.Dataset.from_dataframe(df)` (line 190), iterate the data
variables and stamp the appropriate CF metadata (`mm`/`degC`/`%`/`hPa`,
matching CF standard names where they exist).

### `clip-region` — pass

`scripts/clip.py:67–76` uses `ds.cf["latitude"]` / `ds.cf["longitude"]` to
identify the spatial dims. cf-xarray's accessor matches by `standard_name`,
`units`, `axis`, and a name regex (`latitude|lat|nav_lat`, `longitude|lon|...`),
so inputs that follow the envelope or use the common `lat`/`lon` short names
both resolve. A `--dims LAT,LON` override exists for non-CF inputs.

The `.sel()` operation preserves per-variable and per-coord attrs (xarray
default), and root attrs are explicitly merged (`sub.attrs = {**ds.attrs,
"rhiza_region": ...}`). `.encoding = {}` cleared. CF metadata that was on the
input is therefore also on the output.

### `aggregate-temporal` — **fail** (data-variable attrs lost on reduce)

Input handling is fine: `ds.cf["time"].name` for time-axis ID with a `step`
fallback for forecast lead-time aggregation (line 105–115), and a
`--time-dim` escape hatch.

Output is the problem. The reduce helper at `scripts/aggregate.py:27–33`
calls `grouped.sum() / mean() / max() / min()` **without `keep_attrs=True`**.
xarray's reductions drop per-variable attrs by default (this is the documented
behavior — see `xarray.set_options(keep_attrs=...)` and the `keep_attrs`
parameter on every reduce method). Root attrs are restored manually
(`out_ds.attrs = {**ds.attrs, "rhiza_aggregation": ...}`) but per-variable
`units` / `standard_name` are not.

Result: a chain like `chirps-fetch → aggregate-temporal → plot` produces a
plot with no unit label even though the fetcher set `units = "mm/day"`. This
is a concrete envelope violation, not just a theoretical one.

Fix sketch: either pass `keep_attrs=True` into each reduce call, or wrap the
aggregation in `xr.set_options(keep_attrs=True):`. Apply the same to the
custom `_aggregate_step` path, which uses `_reduce(...)` internally and so
inherits the same loss.

### `downscale` — pass

`scripts/downscale.py:93–102` uses cf-xarray for lat/lon ID with `--dims`
override. The target grid is built explicitly with the input's coord attrs
preserved (line 124–129: `dict(ds[lat_dim].attrs)` / `dict(ds[lon_dim].attrs)`
passed into the new dataset's coord constructors). xarray-regrid's
`.regrid.linear()` is an interpolation (not a reduction) and preserves data
variable attrs by default, so `units` / `standard_name` survive. `.encoding =
{}` cleared. Root attrs merged with `rhiza_downscale_*`.

### `concat` — pass

`scripts/concat.py` does not depend on CF at all — it just concatenates along
a user-provided dim. xarray's `xr.concat(..., dim=...)` preserves attrs from
the first dataset by default, which the script then makes explicit at the root
(`out_ds.attrs = dict(dss[0].attrs)`). Per-variable attrs come along with the
default concat behavior. `.encoding = {}` cleared. Generic by design; nothing
CF-specific to break.

### `plot` — pass

Reads coords via `_cf_dim()` (cf-xarray with `KeyError` → `None`) at
`scripts/plot.py:25–29`. Step-dim detection falls through `("step", "time",
"valid_time")` then to `cf["time"]` (line 78–83), which covers both forecast
and observation envelopes. Variable defaults to first `data_var` if not given.

Uses `da.attrs.get("units", "")` (line 233) for the colorbar label — relies
on CF unit metadata being present, which is exactly the contract. PNG output;
no Zarr-side concerns.

### `plot-compare` — pass (minor note)

`scripts/plot_compare.py:42–49` (`_pick_time_dim`) selects the time dim by
literal name match (`"time"` then `"step"`), not via cf-xarray. That is
narrower than `plot.py`'s detection — a Zarr whose time dim is named e.g.
`valid_time` and only identified via `axis = "T"` would not be picked up
without `--time-dim`. The override exists, so this is a usability nit rather
than a correctness break, and CF-named lat/lon work correctly via `_cf_dim()`.

### `plot-mediogram` — pass (note on dim assumptions)

`scripts/plot_mediogram.py` requires literal `number` and `step` dim names
(line 99–105). That is intentional — the mediogram is defined for
forecast-with-ensemble inputs, which always have those dim names in this
repo's envelope. cf-xarray is used correctly for lat/lon. Variable name
defaults to the first `data_var`. Within its narrow domain it is fine.

### `email-report` — n/a

Not a Zarr producer or consumer; composes an `.eml` from a body and arbitrary
attachments.

## Failures filed as new cards

- `imerg-fetch` does not stamp CF metadata on the `precip` data variable.
- `tahmo-fetch` does not stamp CF metadata on its data variables.
- `aggregate-temporal` drops per-variable attrs through `groupby/resample`
  reductions because reductions are called without `keep_attrs=True`.

Each is filed as a follow-up card under "Part 1: Skills Development" and
linked from this PR's review thread.
