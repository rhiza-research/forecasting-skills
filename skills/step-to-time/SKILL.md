---
name: step-to-time
description: Realize a forecast envelope's `step` lead-time axis as wall-clock valid times (`time = init + step`), replacing the `step` dim with a `time` dim. Use it to compare a forecast against observations — e.g. before plot-compare, plot-timeseries, or difference against a time-based dataset.
license: MIT
compatibility: Requires Python 3.10+ and uv.
metadata:
  version: "0.1.0"
---

# step-to-time

Axis-realization primitive. A forecast envelope labels its temporal axis with
lead times — a `step` dim (`timedelta64`) plus a scalar `time` coord holding
the forecast init date — while observation envelopes carry a wall-clock `time`
dim (`datetime64`). Skills that compare the two need both inputs on the same
kind of axis. This skill computes `valid_time = init + step` and rewrites the
envelope with the `step` dim replaced by a `time` dim labeled with those valid
times.

## When to use

- To compare a forecast against observations: run it on the forecast before
  feeding both inputs to `plot-compare`, `plot-timeseries`, or `difference`
  against a time-based dataset (e.g. CHIRPS, IMERG, station data).
- Whenever a downstream consumer needs the forecast's values labeled by the
  date they are valid for rather than by lead time.

## Input precondition

The input must have a `step` dim whose values are `timedelta64` lead times AND
a scalar (0-d) `time` coord holding the forecast init date (`datetime64`). An
input that already has a `time` dim is already on a wall-clock axis and is
rejected.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/step_to_time.py --input <in.zarr> --output <out.zarr>
```

### Arguments
- `--input`, `-i` — input Zarr containing a forecast envelope with a `step` dim
  and a scalar `time` init coord.
- `--output`, `-o` — output Zarr.

### Output

Same data variables, values, and non-temporal dims/coords (`number`, lat/lon)
as the input, EXCEPT the `step` dim is replaced by a `time` dim of the same
length whose coord holds the realized valid times (`init + step`,
`datetime64`), stamped with CF attrs (`standard_name: time`, `axis: T`). The
scalar `time` init coord and the `step` coord are dropped; the init date is
written to the dataset attr `rhiza_forecast_init` (ISO 8601).

If the input lacks a `step` dim, the `step` values are not `timedelta64`, the
input already has a `time` dim, or there is no scalar `datetime64` `time` init
coord, the skill exits with code 2 and a clear message.

### Provenance

The output stamps a JSON-encoded `rhiza_history` attr: an append-only array
of per-step entries `{skill, version, args, input}`. This skill reads the
upstream input's `rhiza_history` (default `[]` and stderr warning if absent)
and appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings; `input` is a `{basename, hash}` dict —
`basename` is the upstream zarr's filename and `hash` is a sha256 of its
stored bytes, so a renamed-but-unchanged input still cache-hits and a
same-named-but-modified input correctly cache-misses; `version` is the
`_RHIZA_SKILL_VERSION` constant in `scripts/step_to_time.py`, kept in
lockstep with `metadata.version` in this SKILL.md by the CI version-bump
workflow.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/step_to_time.py -i /tmp/ecmwf_daily.zarr -o /tmp/ecmwf_valid.zarr
```
