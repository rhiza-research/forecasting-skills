#!/usr/bin/env python3
"""Golden composition: deaccumulate then aggregate-temporal."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))

from conftest import load_skill, run_skill  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    workdir = args.workdir
    src = workdir / "forecast_tp.zarr"
    out_dir = workdir / "out"
    out_dir.mkdir(exist_ok=True)
    rates = out_dir / "tp_rates.zarr"
    weekly = out_dir / "weekly_rates.zarr"

    deaccumulate = load_skill("deaccumulate", "deaccumulate").deaccumulate
    aggregate = load_skill("aggregate-temporal", "aggregate").aggregate

    run_skill(deaccumulate, "-i", str(src), "-o", str(rates))
    run_skill(aggregate, "-i", str(rates), "-o", str(weekly), "--period", "weekly")
    print(f"wrote {weekly}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
