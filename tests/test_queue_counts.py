"""`bin/queue_counts` (`pantheon/queue/counts.py`): the checking tool the user puts beside Obsidian
to prove the deck's numbers match what he sees on screen. step 2 adds `--base`, so a second
Base file can be checked before switching `pantheon.toml` to point at it."""
from __future__ import annotations

import shutil
from pathlib import Path

from pantheon.queue import counts as counts_mod

FIXTURES = Path(__file__).parent / "fixtures"


def _vault(tmp_path: Path) -> Path:
    projects = tmp_path / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    return tmp_path


def test_default_run_prints_every_tab_and_the_source(tmp_path, capsys):
    vault = _vault(tmp_path)
    rc = counts_mod.main(["--vault", str(vault)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "source: Obsidian Base: Sprints" in out
    assert "Now" in out and "Decide" in out and "By project" in out


def test_spec_flag_uses_the_hand_written_rules(tmp_path, capsys):
    vault = _vault(tmp_path)
    rc = counts_mod.main(["--vault", str(vault), "--spec"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Agent's plate" in out


def test_base_flag_reads_a_different_base_file_with_no_code_change(tmp_path, capsys):
    """acceptance 2 (fixture half): a copy of Sprints.base with one view renamed prints the
    new tab name -- pointing `--base` elsewhere never needs a code change."""
    vault = _vault(tmp_path)
    copy_path = vault / "Projects" / "Copy.base"
    text = (vault / "Projects" / "Sprints.base").read_text(encoding="utf-8")
    copy_path.write_text(text.replace("name: Now", "name: Right now", 1), encoding="utf-8")

    rc = counts_mod.main(["--vault", str(vault), "--base", str(copy_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "source: Obsidian Base: Copy" in out
    assert "Right now" in out


def test_base_flag_follows_a_base_pointed_at_a_different_folder(tmp_path, capsys):
    vault = tmp_path
    other = vault / "Projects" / "Other"
    other.mkdir(parents=True)
    (other / "one.md").write_text(
        '---\nsummary: "Do the other thing"\nproject: other\ndone: false\nnext: true\n---\n',
        encoding="utf-8",
    )
    base = vault / "Projects" / "Other.base"
    base.write_text(
        "views:\n  - type: table\n    name: Now\n    filters:\n      and:\n"
        '        - file.inFolder("Projects/Other")\n        - next == true\n',
        encoding="utf-8",
    )
    rc = counts_mod.main(["--vault", str(vault), "--base", str(base)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "one" in out
    assert str(other) in out   # the printed `folder:` line follows the Base, not the default
