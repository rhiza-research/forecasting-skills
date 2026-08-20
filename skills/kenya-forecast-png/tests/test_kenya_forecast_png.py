"""Correctness tests for kenya-forecast-png (network mocked)."""

import io
from pathlib import Path

import pytest
from conftest import load_skill, run_skill
from PIL import Image
from weather_skills_core.provenance import load_figure_history


def _valid_png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("kenya-forecast-png", "fetch")


def test_fetch_writes_png_and_stamps_history(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "weekly.png"

    monkeypatch.setattr(fetch_mod, "_list_init_dates", lambda: ["2026-01-01", "2026-01-08"])
    monkeypatch.setattr(fetch_mod, "_object_exists", lambda key: True)

    def fake_download(key, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_valid_png_bytes())

    monkeypatch.setattr(fetch_mod, "_download", fake_download)

    run_skill(fetch_mod.fetch, "-o", str(out))

    assert Path(out).exists()
    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "kenya-forecast-png"
    assert history[-1]["args"]["product"] == "weekly_precip.png"


def test_fetch_honors_explicit_date_and_nested_product(tmp_path, fetch_mod, monkeypatch):
    out = tmp_path / "t2m.png"
    seen = {}

    monkeypatch.setattr(
        fetch_mod,
        "_object_exists",
        lambda key: seen.setdefault("key", key) or True,
    )

    def fake_download(key, dest):
        seen["download_key"] = key
        dest.write_bytes(_valid_png_bytes())

    monkeypatch.setattr(fetch_mod, "_download", fake_download)

    run_skill(
        fetch_mod.fetch,
        "-o",
        str(out),
        "--date",
        "2026-01-08",
        "--product",
        "t2m/t2m.png",
    )

    assert seen["key"] == "2026-01-08/weekly/t2m/t2m.png"
    assert seen["download_key"] == "2026-01-08/weekly/t2m/t2m.png"


def test_rejects_path_escape(fetch_mod):
    from weather_skills_core import UsageError

    with pytest.raises(UsageError):
        fetch_mod._normalize_product("../secret.png")
