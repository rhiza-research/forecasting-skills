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
| `imerg-fetch`      | n/a  | pass¹ | n/a      | pass    |
| `tahmo-fetch`      | n/a  | pass¹ | n/a      | pass    |
| `clip-region`      | pass | pass | pass      | pass    |
| `aggregate-temporal` | pass | pass¹ | pass   | pass    |
| `downscale`        | pass | pass | pass      | pass    |
| `concat`           | pass | pass | pass      | pass    |
| `plot`             | pass | n/a  | pass      | pass    |
| `plot-compare`     | note | n/a  | pass      | pass    |
| `plot-mediogram`   | pass | n/a  | note      | pass    |
| `email-report`     | n/a  | n/a  | n/a       | n/a     |

¹ Originally fail; fixed in this PR. See per-skill detail.

11 pass (3 fixed in this PR, 2 with pre-existing notes), 1 n/a.

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

### `imerg-fetch` — pass (fixed in this PR)

Originally failed. `scripts/fetch.py:93` calls `ds = ds.drop_attrs()` after the
rename, stripping both root and per-variable attrs. The script then re-applied
coord CF attrs and root `rhiza_*` attrs but never re-stamped `units` /
`standard_name` on the `precip` data variable. The resulting Zarr had a
`precip` variable with no CF metadata, breaking the envelope claim that "data
variable units should follow CF where possible" and leaving consumers (e.g.
`plot`'s colorbar) with an empty unit label.

**Fix in this PR**: after `_stamp_cf_attrs(ds)` and the root `rhiza_*` update,
stamp `ds["precip"].attrs` with `units = "mm/day"`, `standard_name =
"lwe_thickness_of_precipitation_amount"` (matching the convention already used
for `chirps-fetch`), and `long_name = "IMERG daily precipitation"`. The
GPM_3IMERGDF / GPM_3IMERGDL products report the daily-mean precipitation rate
in mm/day per the GES DISC product specification.

### `tahmo-fetch` — pass (fixed in this PR)

Originally failed. `scripts/fetch.py:196–214` stamped CF attrs on `latitude`,
`longitude`, `time`, `station_id` (`cf_role = "timeseries_id"`, the CF DSG
convention) and set `featureType = "timeSeries"` on the root — correct for the
station envelope — but the data variables (`precip`, `temperature`,
`humidity`, `pressure`) never received `units` or `standard_name`. The
aggregations are done in pandas on raw TAHMO values whose units are not
recorded on the resulting xarray variables. Same envelope violation as
`imerg-fetch`.

**Fix in this PR**: call `api.getVariables()` once (the TAHMO SDK already
exposes a metadata endpoint that returns each shortcode's `units` and
`description`) and use the returned values directly to stamp `units` and
`long_name` on each canonical envelope variable. Pulling from the API rather
than hard-coding keeps the Zarr in sync with whatever TAHMO actually returns.
A small static `CF_STANDARD_NAMES` map adds CF-table-verified standard names
(`lwe_thickness_of_precipitation_amount`, `air_temperature`,
`relative_humidity`, `air_pressure`) on top.

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

### `aggregate-temporal` — pass (fixed in this PR)

Originally failed. Input handling was fine — `ds.cf["time"].name` with a
`step` fallback and a `--time-dim` escape — but the reduce helper at
`scripts/aggregate.py:27–33` called `grouped.sum() / mean() / max() / min()`
without `keep_attrs=True`. xarray reductions drop per-variable attrs by
default, so root attrs were restored manually (`out_ds.attrs = {**ds.attrs,
"rhiza_aggregation": ...}`) but per-variable `units` / `standard_name` were
not. A chain like `chirps-fetch → aggregate-temporal → plot` ended up with no
unit label on the plot even though the fetcher had set `units = "mm/day"`.

**Fix in this PR**: pass `keep_attrs=True` into the single dispatch in
`_reduce()`. The `_aggregate_step` path also routes through `_reduce()`, so
the one change covers both the `time` (xarray resample) and `step` (custom
window) paths.

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

## Failures fixed in this PR

All three originally-failing skills are fixed in the same PR as the audit:

- `imerg-fetch` — stamps `units` / `standard_name` / `long_name` on `precip`
  after `drop_attrs()`.
- `tahmo-fetch` — pulls per-variable units / descriptions from
  `api.getVariables()` and adds CF standard names from a small verified map.
- `aggregate-temporal` — passes `keep_attrs=True` into the reduce dispatch so
  per-variable attrs survive both the resample and step-window paths.
