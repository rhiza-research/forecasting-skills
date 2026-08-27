# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "pillow",
# ]
# ///
"""Print size, blank/uniform flags, a coarse preview, and last provenance of a plot PNG."""

import json
from pathlib import Path

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.provenance import load_figure_history

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_STATS = (160, 100)
_PREVIEW = (16, 10)
_WHITE = 250
_BLACK = 8
_BLANK_FRAC = 0.92
_UNIFORM_UNIQUE = 12
_ARG_KEYS = (
    "style",
    "title",
    "variable",
    "bbox",
    "panels",
    "rows",
    "columns",
    "algorithm",
    "colormap",
    "threshold",
)


def _hex(rgb):
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _pixels(im):
    return list(im.getdata())


def _stats(img):
    from PIL import Image

    rgb = img.convert("RGB")
    sample = rgb.resize(_STATS, resample=Image.Resampling.BOX)
    pixels = _pixels(sample)
    n = len(pixels) or 1
    white = sum(1 for r, g, b in pixels if r >= _WHITE and g >= _WHITE and b >= _WHITE)
    black = sum(1 for r, g, b in pixels if r <= _BLACK and g <= _BLACK and b <= _BLACK)
    unique = len({(r, g, b) for r, g, b in pixels})
    preview = rgb.resize(_PREVIEW, resample=Image.Resampling.BOX)
    rows = []
    w, h = preview.size
    data = _pixels(preview)
    for y in range(h):
        rows.append([_hex(data[y * w + x]) for x in range(w)])
    return {
        "near_white_frac": round(white / n, 4),
        "near_black_frac": round(black / n, 4),
        "unique_colors": unique,
        "looks_blank": (white / n) >= _BLANK_FRAC or (black / n) >= _BLANK_FRAC,
        "looks_uniform": unique <= _UNIFORM_UNIQUE,
        "preview": rows,
    }


def _last_step(path: Path):
    history = load_figure_history(path)
    if not history:
        return None
    step = history[-1]
    if not isinstance(step, dict):
        return None
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    shown = {k: args[k] for k in _ARG_KEYS if k in args}
    return {
        "skill": step.get("skill"),
        "version": step.get("version"),
        "args": shown,
    }


def _notes(payload):
    notes = []
    if payload["looks_blank"]:
        notes.append(
            "mostly empty (near-white or near-black); the field may be all-NaN "
            "or the map extent missed the data — inspect-zarr the input"
        )
    if payload["looks_uniform"] and not payload["looks_blank"]:
        notes.append(
            "few distinct colors; the color scale may be collapsed or the field is constant"
        )
    if min(payload["width"], payload["height"]) < 64:
        notes.append("very small image; the plot may have failed before drawing")
    if payload["last_step"] is None:
        notes.append("no weather_skills_history; use provenance, or the stamp failed")
    return notes


@weather_skill(
    name="inspect-figure",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument("-i", "--input", type=Path, required=True, help="Plot PNG to inspect.")
@weather_skill.argument("--format", choices=["human", "json"], default="human")
def inspect_figure(input, format="human", **kwargs):
    """Print size, blank/uniform flags, a coarse preview, and last provenance of a plot PNG."""
    from PIL import Image

    path = Path(input)
    if not path.exists():
        raise UsageError(f"Error: {path} not found.", prefix=False)
    if not path.is_file() or path.suffix.lower() != ".png":
        raise UsageError(
            f"Error: {path} is not a .png file; inspect-figure reads plot PNGs.",
            prefix=False,
        )
    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
            mode = img.mode
            fmt = img.format or "PNG"
            stats = _stats(img)
    except Exception as exc:  # noqa: BLE001
        raise UsageError(f"Error: could not open {path} as a PNG: {exc}", prefix=False) from None

    payload = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "format": fmt,
        "mode": mode,
        "width": width,
        "height": height,
        **stats,
        "last_step": _last_step(path),
    }
    payload["notes"] = _notes(payload)

    if format == "json":
        print(json.dumps(payload, indent=2))
        return

    last = payload["last_step"]
    print(f"File: {path.name}  {payload['bytes']} bytes")
    print(f"Image: {width} × {height} {mode} {fmt}")
    print(
        f"Fill: {payload['near_white_frac']:.0%} near-white, "
        f"{payload['near_black_frac']:.0%} near-black  "
        f"unique colors (downsampled): {payload['unique_colors']}"
    )
    flags = []
    if payload["looks_blank"]:
        flags.append("BLANK")
    if payload["looks_uniform"]:
        flags.append("UNIFORM")
    print(f"Flags: {', '.join(flags) if flags else 'ok'}")
    if last:
        arg_s = " ".join(f"{k}={v!r}" for k, v in last["args"].items())
        extra = f"  {arg_s}" if arg_s else ""
        print(f"Last skill: {last['skill']} {last['version']}{extra}")
    else:
        print("Last skill: (none)")
    if payload["notes"]:
        print("Notes:")
        for note in payload["notes"]:
            print(f"  - {note}")
    print("Preview:")
    for row in payload["preview"]:
        print("  " + " ".join(row))
    print("For full lineage run provenance; for the Zarr behind the figure run inspect-zarr.")


if __name__ == "__main__":
    inspect_figure()
