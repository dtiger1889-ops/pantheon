"""The standalone task source: a plain folder of Markdown files, no Obsidian
required. `tests/fixtures/standalone/` holds the same 6 fixture notes (byte-identical, no personal
text) used in `tests/fixtures/sprints/`, plus a checked-in `pantheon-tabs.yaml` -- the acceptance-3
parity test below copies those same 6 files into BOTH a standalone folder and a vault fixture and
proves the two sources agree tab for tab.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pantheon import config
from pantheon.models import TaskRow
from pantheon.tasks import obsidian_base as ob
from pantheon.tasks import standalone as sa

FIXTURES = Path(__file__).parent / "fixtures"
STANDALONE_FIXTURE = FIXTURES / "standalone"
SIX_NOTES = [
    "Water the balcony plants.md",
    "Choose a shelving unit for the hallway.md",
    "Relabel the spare backup drive.md",
    "Summarise the workshop notes.md",
    "Look into a rain barrel for the shed.md",
    "Return the borrowed ladder.md",
]


def _folder(tmp_path: Path, with_tabs: bool = True) -> Path:
    """A throwaway copy of the standalone fixture folder."""
    dest = tmp_path / "pantheon-tasks"
    dest.mkdir(parents=True)
    for name in SIX_NOTES:
        shutil.copy(STANDALONE_FIXTURE / name, dest / name)
    if with_tabs:
        shutil.copy(STANDALONE_FIXTURE / "pantheon-tabs.yaml", dest / "pantheon-tabs.yaml")
    return dest


def _source(tmp_path: Path, with_tabs: bool = True) -> sa.StandaloneSource:
    cfg = config.Config(standalone={"folder": str(_folder(tmp_path, with_tabs))})
    return sa.make(cfg)


@pytest.fixture
def source(tmp_path):
    return _source(tmp_path)


# ---------------------------------------------------------------- reading rows


def test_reads_every_fixture_note(source):
    rows = source.rows()
    assert len(rows) == 6
    assert source.parse_errors() == 0
    by_id = {r.id: r for r in rows}
    plants = by_id["Water the balcony plants"]
    assert plants.next is True and plants.project == "household"
    assert plants.path.endswith("Water the balcony plants.md")


def test_kind_and_name(source):
    assert source.name == "standalone"
    assert source.kind == f"folder: {source.folder.name}"


def test_open_for_edit_returns_the_files_own_path(source):
    row = next(r for r in source.rows() if r.id == "Water the balcony plants")
    assert source.open_for_edit(row) == row.path
    assert source.open_for_edit(row).endswith("Water the balcony plants.md")


def test_a_half_written_file_keeps_the_last_good_copy(tmp_path):
    src = _source(tmp_path)
    note = src.folder / "Water the balcony plants.md"
    before = next(r for r in src.rows() if r.id == "Water the balcony plants").summary

    note.write_text("---\nsummary: \"half a fi", encoding="utf-8")
    src.refresh()

    assert src.parse_errors() >= 1
    assert "Water the balcony plants.md" in src.error_files()
    kept = next(r for r in src.rows() if r.id == "Water the balcony plants")
    assert kept.summary == before
    assert len(src.rows()) == 6


def test_a_missing_folder_is_created_rather_than_crashing(tmp_path):
    folder = tmp_path / "brand-new"
    cfg = config.Config(standalone={"folder": str(folder)})
    src = sa.make(cfg)
    assert folder.is_dir()
    assert src.rows() == []
    assert src.load_error == ""


# ---------------------------------------------------------------- the starter tabs file


def test_a_missing_tabs_file_gets_a_starter_written_out(tmp_path):
    src = _source(tmp_path, with_tabs=False)
    tabs_path = src.folder / sa.TABS_FILENAME
    assert tabs_path.exists()
    assert tabs_path.read_text(encoding="utf-8") == sa.STARTER_TABS
    names = [t.name for t in src.tabs()]
    assert names == ["Now", "Decide", "Quick wins", "Agent's plate", "Someday", "Notes", "By project"]


def test_the_written_starter_tabs_keys_are_1_through_7(tmp_path):
    src = _source(tmp_path, with_tabs=False)
    keys = {t.name: t.key for t in src.tabs()}
    assert keys["Now"] == "1" and keys["Decide"] == "2" and keys["By project"] == "7"


def test_an_existing_tabs_file_is_read_as_is_not_overwritten(tmp_path):
    folder = _folder(tmp_path, with_tabs=False)
    custom = 'tabs:\n  - name: Everything\n    key: "1"\n    filters: { and: ["done != true"] }\n'
    (folder / sa.TABS_FILENAME).write_text(custom, encoding="utf-8")
    cfg = config.Config(standalone={"folder": str(folder)})
    src = sa.make(cfg)
    assert [t.name for t in src.tabs()] == ["Everything"]
    assert (folder / sa.TABS_FILENAME).read_text(encoding="utf-8") == custom


def test_an_unknown_rule_in_a_tabs_file_is_flagged_not_fatal(tmp_path):
    folder = _folder(tmp_path, with_tabs=False)
    custom = (
        'tabs:\n'
        '  - name: Weird\n'
        '    key: "1"\n'
        '    filters: { and: ["done != true", "summary.contains(\\"x\\")"] }\n'
    )
    (folder / sa.TABS_FILENAME).write_text(custom, encoding="utf-8")
    src = sa.make(config.Config(standalone={"folder": str(folder)}))
    assert src.flagged_tabs() == {"Weird"}
    tab = src.tabs()[0]
    assert len([r for r in src.rows() if tab.filter(r)]) == 5   # everyone except the finished row


# ---------------------------------------------------------------- refresh


def test_refresh_does_nothing_when_the_folder_has_not_changed(source):
    """: no folder watcher -- `refresh` is cheap when nothing moved."""
    assert source.refresh() is False


def test_refresh_picks_up_a_new_file(tmp_path):
    src = _source(tmp_path)
    before = len(src.rows())
    (src.folder / "New task.md").write_text(
        '---\nsummary: "A brand new task"\nproject: workshop\ndone: false\nnext: true\n---\n',
        encoding="utf-8",
    )
    assert src.refresh() is True
    assert len(src.rows()) == before + 1


# ---------------------------------------------------------------- acceptance 3: parity with Obsidian


def _vault_with_the_same_six_notes(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    sprints = vault / "Projects" / "Sprints"
    sprints.mkdir(parents=True)
    for name in SIX_NOTES:
        shutil.copy(STANDALONE_FIXTURE / name, sprints / name)
    shutil.copy(FIXTURES / "Sprints.base", vault / "Projects" / "Sprints.base")
    return vault


def test_the_standalone_source_agrees_with_the_obsidian_source_on_the_same_six_files(tmp_path):
    standalone_src = _source(tmp_path)
    obsidian_src = ob.make(config.Config(vault=str(_vault_with_the_same_six_notes(tmp_path))))

    standalone_names = {t.name for t in standalone_src.tabs()}
    obsidian_names = {t.name for t in obsidian_src.tabs()}
    shared = standalone_names & obsidian_names
    assert shared == {"Now", "Decide", "Quick wins", "Agent's plate", "Someday", "Notes", "By project"}

    disagreements = []
    for name in shared:
        s_tab = next(t for t in standalone_src.tabs() if t.name == name)
        o_tab = next(t for t in obsidian_src.tabs() if t.name == name)
        from_standalone = {r.id for r in standalone_src.rows() if s_tab.filter(r)}
        from_obsidian = {r.id for r in obsidian_src.rows() if o_tab.filter(r)}
        if from_standalone != from_obsidian:
            disagreements.append((name, len(from_standalone), len(from_obsidian)))
    assert not disagreements, f"standalone and Obsidian disagree: {disagreements}"
    # and the counts are not trivially all-empty or all-everyone
    assert len({r.id for r in standalone_src.rows()
               if next(t for t in standalone_src.tabs() if t.name == "Now").filter(r)}) == 1
