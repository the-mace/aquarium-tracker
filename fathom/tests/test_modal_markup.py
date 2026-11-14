import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _templates():
    return {p: p.read_text() for p in TEMPLATES.rglob("*.html")}


def test_modals_are_native_dialogs():
    for path, text in _templates().items():
        assert not re.search(r'<div[^>]*class="modal"', text), f"{path}: use <dialog class=\"modal\">"


def test_dialogs_are_labelled_by_their_heading():
    for path, text in _templates().items():
        for m in re.finditer(r'<dialog id="([^"]+)" class="modal"([^>]*)>', text):
            label = re.search(r'aria-labelledby="([^"]+)"', m.group(2))
            assert label, f"{path}: #{m.group(1)} has no aria-labelledby"
            assert f'id="{label.group(1)}"' in text, f"{path}: #{m.group(1)} label target missing"


def test_command_buttons_target_existing_dialogs():
    for path, text in _templates().items():
        for target in re.findall(r'command="(?:show-modal|close)" commandfor="([^"]+)"', text):
            assert f'<dialog id="{target}"' in text, f"{path}: commandfor={target} has no dialog"
