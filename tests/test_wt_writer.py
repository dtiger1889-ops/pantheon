"""`pantheon/appearance/wt.py` against a fixture Windows Terminal
`settings.json` -- never the real file (HARD SAFETY: this suite only ever touches
`tests/fixtures/appearance/wt_settings.jsonc`, copied to a tmp dir first)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pantheon.appearance import wt

FIXTURE = Path(__file__).parent / "fixtures" / "appearance" / "wt_settings.jsonc"


@pytest.fixture()
def settings_copy(tmp_path):
    dst = tmp_path / "settings.json"
    shutil.copy2(FIXTURE, dst)
    return dst


def _strip_comments_and_load(path: Path) -> dict:
    return json.loads(wt._blank_comments(path.read_text(encoding="utf-8")))


def test_read_font_matched_profile(settings_copy):
    font = wt.read_font(settings_copy, "Pantheon")
    assert font.face == "Cascadia Mono"
    assert font.size == 11
    assert font.cell_height is None
    assert font.cell_width is None


def test_write_font_changes_only_the_four_keys(settings_copy, tmp_path):
    before = settings_copy.read_text(encoding="utf-8")
    report = wt.write_font(
        settings_copy, "Pantheon",
        wt.Font(size=13, cell_width=1.1),
        state_dir=tmp_path,
    )
    after = settings_copy.read_text(encoding="utf-8")

    # Only the size value and one new key differ; everything else is byte-identical.
    assert "Pantheon" in after and "Windows PowerShell" in after
    assert '"guid": "{11111111-1111-1111-1111-111111111111}"' in after
    assert "// This is the user's Windows Terminal settings.json" in after
    assert "/* the deck's profile */" in after
    assert "$schema" in after

    font = wt.read_font(settings_copy, "Pantheon")
    assert font.size == 13
    assert font.cell_width == 1.1
    assert font.face == "Cascadia Mono"  # untouched
    assert font.cell_height is None  # untouched

    # A backup was made and matches the pre-write content exactly.
    assert report["backup"].exists()
    assert report["backup"].read_text(encoding="utf-8") == before
    assert report["profile"] == "matched"


def test_write_font_keeps_every_comment_and_key_order(settings_copy, tmp_path):
    before_lines = [l for l in settings_copy.read_text(encoding="utf-8").splitlines() if l.strip()]
    wt.write_font(settings_copy, "Pantheon", wt.Font(face="JetBrains Mono"), state_dir=tmp_path)
    after_lines = [l.strip() for l in settings_copy.read_text(encoding="utf-8").splitlines() if l.strip()]
    before_stripped = [l.strip() for l in before_lines]
    # Every original line is still present, in order, plus exactly the touched font line changed.
    changed = [l for l in after_lines if l not in before_stripped]
    assert len(changed) == 1
    assert '"face": "JetBrains Mono"' in changed[0]
    # The result still parses.
    _strip_comments_and_load(settings_copy)


def test_write_font_result_still_parses(settings_copy, tmp_path):
    wt.write_font(settings_copy, "Pantheon", wt.Font(size=14, cell_height=1.2, cell_width=0.9), state_dir=tmp_path)
    data = _strip_comments_and_load(settings_copy)
    assert data["$schema"] == "https://aka.ms/terminal-profiles-schema"


def test_missing_profile_falls_back_to_defaults_with_a_message(settings_copy, tmp_path):
    report = wt.write_font(settings_copy, "No Such Profile", wt.Font(face="Consolas"), state_dir=tmp_path)
    assert report["profile"] == "defaults"
    font = wt.read_font(settings_copy, "No Such Profile")
    assert font.face == "Consolas"


def test_missing_file_returns_none_and_writes_nothing(tmp_path):
    missing = tmp_path / "no-such-file.json"
    assert wt.find_settings(tmp_path / "does-not-exist") is None
    assert not missing.exists()


def test_find_settings_globs_the_hashed_package_dir(tmp_path):
    pkg = tmp_path / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "LocalState"
    pkg.mkdir(parents=True)
    target = pkg / "settings.json"
    target.write_text("{}", encoding="utf-8")
    found = wt.find_settings(tmp_path)
    assert found == target


def test_find_settings_none_when_packages_dir_absent(tmp_path):
    assert wt.find_settings(tmp_path / "nope") is None


def test_write_font_restores_backup_on_verification_failure(settings_copy, tmp_path, monkeypatch):
    before = settings_copy.read_text(encoding="utf-8")

    def fake_read_font(path, profile_name):
        return wt.Font(face="not what we wrote")

    monkeypatch.setattr(wt, "read_font", fake_read_font)
    with pytest.raises(wt.WriteError):
        wt.write_font(settings_copy, "Pantheon", wt.Font(face="JetBrains Mono"), state_dir=tmp_path)
    assert settings_copy.read_text(encoding="utf-8") == before


def test_write_font_no_keys_is_a_noop(settings_copy, tmp_path):
    before = settings_copy.read_text(encoding="utf-8")
    report = wt.write_font(settings_copy, "Pantheon", wt.Font(), state_dir=tmp_path)
    assert report["changes"] == []
    assert settings_copy.read_text(encoding="utf-8") == before
