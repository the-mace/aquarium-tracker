import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[2] / "scripts" / "is_patch_bump.py"
    spec = importlib.util.spec_from_file_location("is_patch_bump", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_patch_titles():
    is_patch_bump = _load().is_patch_bump
    assert is_patch_bump("deps: bump uvicorn from 0.52.0 to 0.52.1") is True
    assert is_patch_bump("Bump actions/checkout from 4.1.1 to 4.1.2") is True


def test_minor_and_major_not_patch():
    is_patch_bump = _load().is_patch_bump
    assert is_patch_bump("deps: bump anthropic from 0.120.2 to 0.121.0") is False
    assert is_patch_bump("ci: bump actions/checkout from 4 to 7") is False
    assert is_patch_bump("Bump jinja2 from 3.1.6 to 4.0.0") is False


def test_unparseable_title():
    is_patch_bump = _load().is_patch_bump
    assert is_patch_bump("chore: something else") is None
    assert is_patch_bump("") is None
