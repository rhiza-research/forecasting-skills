---
name: zonal-moisture-transport
description: Multiply specific humidity by zonal wind (q·u) and optionally integrate through the pressure column to eastward IVT (`viwve`, kg m-1 s-1). Use after ecmwf-fetch `-v q` and `-v u` (or any standard dataset that holds both fields) when you need zonal moisture transport maps.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/zonal_moisture_transport.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# zonal-moisture-transport

Eastward moisture flux from specific humidity × zonal wind. Default is the
column integral used for IVT-style maps (ECMWF `viwve`):

```
qu = q · u
IVT_x = (1/g) ∫ qu dp     g = 9.80665 m s-2,  dp in Pa
```

The integral is trapezoidal along the ontology `vertical` dim (also `level`,
`isobaricInhPa`, …). Pressure coords in hPa are converted to Pa. Shared dims
are inner-joined, so a 7-level `q` stack and a 10-level `u` stack keep the
overlapping levels only.

`--no-integrate` writes the per-level product `qu` (`kg kg-1 m s-1`) instead
— use that after `select` has already sliced one pressure (e.g. 850 hPa).

## When to use

- Eastward IVT / zonal moisture-transport maps from ECMWF S2S pressure-level
  `q` and `u` (`ecmwf-fetch` writes those as `q` and `u` on `vertical`).
- The same product from any other standard dataset that carries specific
  humidity and zonal wind (ERA5, a reforecast, …).
- A single-level moisture flux after `select` on `vertical` (`--no-integrate`).

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/zonal_moisture_transport.py -i <q_and_or_u.zarr> \
    [--input <u.zarr>] --output <out.zarr> [--no-integrate] \
    [--humidity-variable VAR] [--wind-variable VAR] [--output-name NAME]
```

The output must be a distinct store from every input.

### Arguments

- `--input`, `-i` — input Zarr; pass once or twice. One path may hold both
  `q` and `u`. Two paths are humidity first, wind second. Any other count
  exits non-zero.
- `--output`, `-o` — output Zarr.
- `--humidity-variable` — specific-humidity name. Default: first of `q`,
  `specific_humidity`, `hus`, `humidity` found in the inputs. With two
  inputs and no recognized name, the first input's only data variable is used.
- `--wind-variable` — zonal-wind name. Default: first of `u`,
  `u_component_of_wind`, `ua`, `uwnd`, `eastward_wind`. With two inputs and
  no recognized name, the second input's only data variable is used.
- `--integrate` / `--no-integrate` — column-integrate (default) or keep
  per-level `q*u`. `--integrate` exits non-zero unless a pressure dim has at
  least two finite levels.
- `--output-name` — output variable name. Default `viwve` (integrated) or
  `qu` (per-level).

### Output

| Flag | Variable | Units | Meaning |
|---|---|---|---|
| `--integrate` (default) | `viwve` | `kg m-1 s-1` | Eastward IVT, `(1/g) ∫ q u dp` |
| `--no-integrate` | `qu` | `kg kg-1 m s-1` | Per-level eastward moisture flux |

Integrated output drops the vertical dim. Dataset attrs from the first input
are preserved. Positive `viwve` is eastward transport.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr. Inspect it
with the `provenance` skill.

## Examples

```bash
# ECMWF S2S: fetch humidity and zonal wind (two ECDS legs; q is 7 levels, u is 10),
# then column-integrate to eastward IVT.
uv run ${CLAUDE_SKILL_DIR}/../ecmwf-fetch/scripts/fetch.py --date 2026-08-30 \
    --bbox 5/30/-5/50 -v q -v u --output /tmp/ecmwf_q_u.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/zonal_moisture_transport.py \
    -i /tmp/ecmwf_q_u.zarr --output /tmp/viwve.zarr
```

```bash
# Two stores (humidity, then wind) — same integral.
uv run ${CLAUDE_SKILL_DIR}/scripts/zonal_moisture_transport.py \
    -i /tmp/q.zarr -i /tmp/u.zarr --output /tmp/viwve.zarr
```

```bash
# 850 hPa moisture flux only (select the level first).
uv run ${CLAUDE_SKILL_DIR}/scripts/zonal_moisture_transport.py \
    -i /tmp/q_u_850.zarr --no-integrate --output /tmp/qu_850.zarr
```
