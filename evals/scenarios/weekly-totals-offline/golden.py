#!/usr/bin/env python3
"""Golden composition for weekly-totals-offline (no LLM)."""

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
    rates = workdir / "rates.zarr"
    out_dir = workdir / "out"
    out_dir.mkdir(exist_ok=True)
    weekly = out_dir / "weekly_rates.zarr"
    totals = out_dir / "weekly_totals.zarr"

    aggregate = load_skill("aggregate-temporal", "aggregate").aggregate
    convert = load_skill("convert-to-totals", "convert_to_totals").convert_to_totals

    run_skill(
        aggregate,
        "-i",
        str(rates),
        "-o",
        str(weekly),
        "--period",
        "weekly",
        "--end-time",
        "2026-08-30",
    )
    run_skill(convert, "-i", str(weekly), "-o", str(totals))
    print(f"wrote {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
