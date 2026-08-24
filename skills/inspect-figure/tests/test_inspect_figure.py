"""Correctness tests for inspect-figure."""

import json

import pytest
from conftest import load_skill, run_skill
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from weather_skills_core.provenance import HISTORY_ATTR


@pytest.fixture(scope="module")
def inspect_figure():
    return load_skill("inspect-figure", "inspect_figure").inspect_figure


def _png(path, color=None, size=(80, 60), history=None, varied=False):
    img = Image.new("RGB", size, color=color or (0, 0, 0))
    if varied:
        px = img.load()
        for y in range(size[1]):
            for x in range(size[0]):
                px[x, y] = (x % 256, y % 256, (x * 3 + y * 5) % 256)
    info = PngInfo()
    if history is not None:
        info.add_text(HISTORY_ATTR, json.dumps(history))
    img.save(path, pnginfo=info)
    return path


def test_human_reports_size_and_flags_ok(tmp_path, inspect_figure, capsys):
    src = _png(tmp_path / "map.png", size=(120, 80), varied=True)

    run_skill(inspect_figure, "-i", str(src))

    out = capsys.readouterr().out
    assert "120 × 80" in out
    assert "Flags: ok" in out
    assert "Preview:" in out


def test_white_image_is_blank(tmp_path, inspect_figure, capsys):
    src = _png(tmp_path / "blank.png", (255, 255, 255), size=(100, 80))

    run_skill(inspect_figure, "-i", str(src), "--format", "json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["looks_blank"] is True
    assert payload["width"] == 100
    assert any("empty" in n for n in payload["notes"])


def test_json_includes_last_skill(tmp_path, inspect_figure, capsys):
    history = [
        {
            "skill": "plot",
            "version": "0.0.2",
            "args": {"style": "heatmap", "title": "Precip", "ignored": 1},
            "input": {"basename": "in.zarr", "hash": "abc"},
        }
    ]
    src = _png(tmp_path / "stamped.png", color=(10, 80, 10), size=(120, 80), history=history)

    run_skill(inspect_figure, "-i", str(src), "--format", "json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["last_step"]["skill"] == "plot"
    assert payload["last_step"]["args"] == {"style": "heatmap", "title": "Precip"}
    assert "ignored" not in payload["last_step"]["args"]


def test_missing_file_exits_2(tmp_path, inspect_figure):
    with pytest.raises(SystemExit) as exc:
        run_skill(inspect_figure, "-i", str(tmp_path / "nope.png"))
    assert exc.value.code == 2


def test_rejects_zarr_directory(tmp_path, inspect_figure):
    zarr = tmp_path / "in.zarr"
    zarr.mkdir()
    with pytest.raises(SystemExit) as exc:
        run_skill(inspect_figure, "-i", str(zarr))
    assert exc.value.code == 2
