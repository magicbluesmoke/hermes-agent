import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parsing import parse_output, resolve_dimensions


def test_parse_output_image_path_direct():
    out = {"image_path": "/tmp/a.png", "seed": 42, "steps": 25}
    r = parse_output(out)
    assert r["image_path"] == "/tmp/a.png"
    assert r["seed"] == 42
    assert r["steps"] == 25


def test_parse_output_falls_back_to_outputs_file():
    out = {"outputs": [{"file": "/tmp/b.png"}], "seed": 7}
    r = parse_output(out)
    assert r["image_path"] == "/tmp/b.png"
    assert r["seed"] == 7
    assert r["steps"] is None


def test_parse_output_empty_returns_none():
    r = parse_output({})
    assert r["image_path"] is None
    assert r["seed"] is None
    assert r["steps"] is None


def test_parse_output_prefers_image_path_over_outputs():
    out = {"image_path": "/tmp/direct.png", "outputs": [{"file": "/tmp/other.png"}]}
    assert parse_output(out)["image_path"] == "/tmp/direct.png"


def test_resolve_dimensions_known():
    assert resolve_dimensions("landscape") == (1024, 768)
    assert resolve_dimensions("square") == (1024, 1024)
    assert resolve_dimensions("portrait") == (768, 1024)


def test_resolve_dimensions_unknown_defaults_square():
    assert resolve_dimensions("bogus") == (1024, 1024)
