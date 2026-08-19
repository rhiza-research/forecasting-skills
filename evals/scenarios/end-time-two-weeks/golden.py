#!/usr/bin/env python3
"""Golden: weekly aggregate with --end-time yields two complete bins."""

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
    out = workdir / "out"
    out.mkdir(exist_ok=True)
    weekly = out / "weekly.zarr"

    aggregate = load_skill("aggregate-temporal", "aggregate").aggregate
    run_skill(
        aggregate,
        "-i",
        str(workdir / "rates.zarr"),
        "-o",
        str(weekly),
        "--period",
        "weekly",
        "--end-time",
        "2026-08-30",
    )
    print(f"wrote {weekly}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
