# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pyyaml",
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@combine-dim-ontology-cleanup",
# ]
# ///
"""Build resolve-time's product catalog from SKILL.md metadata.availability.

Walks skills/*/SKILL.md, requires `metadata.availability` on every
`catalog-group: fetchers` skill, flattens `variants` to `name:variant` keys,
and writes skills/resolve-time/assets/products.json.

Usage:
    uv run tools/build_availability.py              # write the snapshot
    uv run tools/build_availability.py --check      # exit 1 if stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"
SNAPSHOT = SKILLS_DIR / "resolve-time" / "assets" / "products.json"
_SIBLING_CORE = ROOT.parent / "weather-skills-core" / "src"

if _SIBLING_CORE.is_dir():
    sys.path.insert(0, str(_SIBLING_CORE))

from weather_skills_core.availability import Availability
from weather_skills_core.errors import UsageError

_GENERATED_BY = "tools/build_availability.py"


def _parse_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise ValueError(f"{skill_md}: no YAML frontmatter")
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            block = "\n".join(lines[1:index])
            break
    else:
        raise ValueError(f"{skill_md}: frontmatter has no closing `---` line")
    # Descriptions are unquoted one-liners and often contain ": ", which YAML
    # treats as a nested mapping. Quote that field so the rest of the block
    # (metadata.availability) still round-trips through SafeLoader.
    quoted = []
    for line in block.split("\n"):
        if line.startswith("description:"):
            value = line[len("description:") :].strip()
            quoted.append("description: " + json.dumps(value))
        else:
            quoted.append(line)
    try:
        data = yaml.safe_load("\n".join(quoted))
    except yaml.YAMLError as exc:
        raise ValueError(f"{skill_md}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{skill_md}: frontmatter is not a mapping")  # noqa: TRY004 -- ValueError is the documented malformed-frontmatter contract; not a type bug
    return data


def _merge(base: dict, override: dict | None) -> dict:
    out = {k: v for k, v in base.items() if k != "variants"}
    for key, value in (override or {}).items():
        if key == "variants":
            continue
        out[key] = value
    return out


def _spec_dict(raw: dict, *, origin: str) -> dict:
    try:
        return Availability.from_dict(raw).to_dict()
    except UsageError as exc:
        raise ValueError(f"{origin}: {exc}") from None


def collect_products(skills_dir: Path) -> dict[str, dict]:
    products: dict[str, dict] = {}
    skill_mds = sorted(skills_dir.glob("*/SKILL.md"), key=lambda p: p.parent.name)
    if not skill_mds:
        raise ValueError(f"no skills/*/SKILL.md found under {skills_dir}")
    for skill_md in skill_mds:
        front = _parse_frontmatter(skill_md)
        name = front.get("name")
        if name != skill_md.parent.name:
            raise ValueError(
                f"{skill_md}: frontmatter name {name!r} != directory {skill_md.parent.name!r}"
            )
        metadata = front.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"{skill_md}: frontmatter has no metadata map")  # noqa: TRY004 -- ValueError is the documented malformed-frontmatter contract; not a type bug
        group = metadata.get("catalog-group")
        avail = metadata.get("availability")
        if group == "fetchers" and not avail:
            raise ValueError(f"{skill_md}: catalog-group fetchers requires metadata.availability")
        if not avail:
            continue
        if not isinstance(avail, dict):
            raise ValueError(f"{skill_md}: metadata.availability is not a mapping")  # noqa: TRY004 -- ValueError is the documented malformed-frontmatter contract; not a type bug
        variants = avail.get("variants") or {}
        if variants and not isinstance(variants, dict):
            raise ValueError(f"{skill_md}: metadata.availability.variants is not a mapping")
        products[name] = _spec_dict(_merge(avail, None), origin=f"{skill_md} ({name})")
        for variant, override in variants.items():
            if override is None:
                override = {}
            if not isinstance(override, dict):
                raise ValueError(  # noqa: TRY004 -- ValueError is the documented malformed-frontmatter contract; not a type bug
                    f"{skill_md}: variant {variant!r} must be a mapping (or empty)"
                )
            key = f"{name}:{variant}"
            products[key] = _spec_dict(_merge(avail, override), origin=f"{skill_md} ({key})")
    return dict(sorted(products.items()))


def snapshot_payload(products: dict[str, dict]) -> dict:
    return {"generated_by": _GENERATED_BY, "products": products}


def snapshot_text(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed snapshot does not match SKILL.md",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=SNAPSHOT,
        help=f"snapshot path (default {SNAPSHOT.relative_to(ROOT)})",
    )
    args = parser.parse_args(argv)
    try:
        products = collect_products(SKILLS_DIR)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    text = snapshot_text(snapshot_payload(products))
    out: Path = args.output
    if args.check:
        if not out.is_file():
            print(f"Error: snapshot missing: {out}", file=sys.stderr)
            return 1
        current = out.read_text(encoding="utf-8")
        if current != text:
            print(
                f"Error: {out.relative_to(ROOT)} is stale. "
                "Run `uv run tools/build_availability.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{out.relative_to(ROOT)} is up to date ({len(products)} products).")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"Wrote {out.relative_to(ROOT)} ({len(products)} products).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
