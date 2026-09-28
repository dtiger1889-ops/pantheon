"""Reading the vault: do the tabs hold the right tasks, and does a half-written file break it?

Two rule sets are checked against each other on every test: the ones read out of the Base file
and the ones hand-copied from the specs into `pantheon/queue/filters.py`. They must agree.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pantheon import config
from pantheon.models import TaskRow
from pantheon.queue import filters as F
from pantheon.tasks import basefilter as bf
from pantheon.tasks import obsidian_base as ob

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- setup


def _vault(tmp_path: Path) -> Path:
    """A throwaway vault laid out the way the real one is: Projects/Sprints plus Sprints.base."""
    projects = tmp_path / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    return tmp_path


def _source(tmp_path: Path) -> ob.ObsidianBaseSource:
    cfg = config.Config(vault=str(_vault(tmp_path)))
    return ob.make(cfg)


@pytest.fixture
def source(tmp_path):
    return _source(tmp_path)


# ---------------------------------------------------------------- reading a note


def test_reads_every_fixture(source):
    rows = source.rows()
    assert len(rows) == 9
    assert source.parse_errors() == 0
    by_id = {r.id: r for r in rows}
    plants = by_id["Water the balcony plants"]
    assert plants.next is True and plants.agent is False
    assert plants.project == "household" and plants.picked == "2026-08-20"
    assert plants.path.endswith("Water the balcony plants.md")
    assert plants.body.startswith("The trays hold water")


def test_booleans_written_as_words_become_real_yes_no(source):
    row = next(r for r in source.rows() if r.id == "Relabel the spare backup drive")
    assert row.agent is False and row.next is False   # the file says "false" in quotes
    assert F.quick_wins(row) is True                  # so it counts as a quick win


def test_dates_and_lists_come_back_tidy(source):
    row = next(r for r in source.rows() if r.id == "Summarise the workshop notes")
    assert row.created == "2026-08-05"
    assert isinstance(row.project_assignment_log, list) and len(row.project_assignment_log) == 2
    assert all(isinstance(line, str) for line in row.project_assignment_log)
    assert ob.strip_wikilink(row.source) == "Workshop notes index"


def test_missing_values_are_empty_not_guessed(source):
    row = next(r for r in source.rows() if r.id == "Trial a seed-swap listing site")
    assert row.est_context is None and row.due is None and row.picked is None
    assert F.plate_group(row) == "unsized"


# ---------------------------------------------------------------- a half-written file


def test_a_half_written_file_keeps_the_last_good_copy(tmp_path):
    src = _source(tmp_path)
    note = Path(src.folder) / "Water the balcony plants.md"
    before = next(r for r in src.rows() if r.id == "Water the balcony plants").summary

    note.write_text("---\nsummary: \"half a fi", encoding="utf-8")   # a sync caught mid-write
    src.refresh()

    assert src.parse_errors() >= 1
    assert "Water the balcony plants.md" in src.error_files()
    kept = next(r for r in src.rows() if r.id == "Water the balcony plants")
    assert kept.summary == before        # the old copy is still on screen
    assert len(src.rows()) == 9          # nothing vanished


def test_a_missing_folder_says_so_instead_of_crashing(tmp_path):
    cfg = config.Config(vault=str(tmp_path / "not there"))
    src = ob.make(cfg)
    assert src.rows() == []
    assert "fix it in pantheon.toml" in src.load_error
    assert src.refresh() is False


# ---------------------------------------------------------------- every tab, both ways


POSITIVE = {
    "Now": "Water the balcony plants",
    "Decide": "Choose a shelving unit for the hallway",
    "Quick wins": "Relabel the spare backup drive",
    "Agent's plate": "Summarise the workshop notes",
    "Someday": "Look into a rain barrel for the shed",
    "Notes": "Choose a shelving unit for the hallway",
    "By project": "Water the balcony plants",
}
NEGATIVE = {
    "Now": "Relabel the spare backup drive",          # `next` is the word "false"
    "Decide": "Rebuild the photo archive index",      # blocked, but it is Claude's row
    "Quick wins": "Water the balcony plants",         # already picked up, so it is on Now
    "Agent's plate": "Choose a shelving unit for the hallway",
    "Someday": "Water the balcony plants",
    "Notes": "Return the borrowed ladder",            # finished work never shows
    "By project": "Return the borrowed ladder",
}


@pytest.mark.parametrize("tab", F.TAB_ORDER)
def test_each_tab_has_a_row_it_keeps_and_a_row_it_drops(source, tab):
    ids = {r.id for r in F.select(source.rows(), tab)}
    assert POSITIVE[tab] in ids, f"{tab} should have kept {POSITIVE[tab]}"
    assert NEGATIVE[tab] not in ids, f"{tab} should have dropped {NEGATIVE[tab]}"


def test_finished_work_is_on_no_tab_at_all(source):
    for tab in F.TAB_ORDER:
        assert "Return the borrowed ladder" not in {r.id for r in F.select(source.rows(), tab)}


def test_the_parked_and_blocked_row_stays_off_decide(source):
    """Blocked plus parked is not a decision waiting on anyone (`tier != "someday"`)."""
    ids = {r.id for r in F.select(source.rows(), "Decide")}
    assert "Look into a rain barrel for the shed" not in ids
    assert "Look into a rain barrel for the shed" in {r.id for r in F.select(source.rows(), "Someday")}


def test_the_base_file_and_the_specs_agree_on_the_fixtures(source):
    rows = source.rows()
    spec_counts = F.counts(rows)
    for tab in source.tabs():
        if tab.name not in spec_counts:
            continue        # a view the user added to his Base that the specs never named
        from_base = {r.id for r in rows if tab.filter(r)}
        from_spec = {r.id for r in F.select(rows, tab.name)}
        assert from_base == from_spec, f"{tab.name}: Base file and specs disagree"


# ---------------------------------------------------------------- tabs and grouping


def test_the_number_keys_are_the_ones_written_on_the_launcher(source):
    keys = {tab.name: tab.key for tab in source.tabs()}
    assert keys["Now"] == "1" and keys["Decide"] == "2" and keys["Quick wins"] == "3"
    assert keys["Agent's plate"] == "4" and keys["Someday"] == "5"
    assert keys["Notes"] == "6" and keys["By project"] == "7"
    # A view in the Base the specs never named keeps a number, but only after the named ones.
    assert keys.get("My queue") == "8"


def test_the_plate_tab_groups_by_size_in_the_order_the_spec_asks_for(source):
    tab = next(t for t in source.tabs() if t.name == "Agent's plate")
    assert tab.group_order == ["small", "medium", "large", "unsized"]
    kept = sorted([r for r in source.rows() if tab.filter(r)], key=tab.sort_key)
    grouped = {}
    for row in kept:
        grouped.setdefault(tab.group_by(row), []).append(row.id)
    assert grouped["small"] == ["Summarise the workshop notes"]
    assert grouped["medium"] == ["Write up the tool-hire comparison"]
    assert grouped["large"] == ["Rebuild the photo archive index"]
    assert grouped["unsized"] == ["Trial a seed-swap listing site"]


def test_plate_group_names(source):
    assert F.plate_group(TaskRow(id="x", est_context="MEDIUM")) == "medium"
    assert F.plate_group(TaskRow(id="x", est_context="enormous")) == "unsized"
    assert F.plate_group(TaskRow(id="x")) == "unsized"


def test_someday_puts_the_users_rows_before_claudes(source):
    ids = [r.id for r in F.select(source.rows(), "Someday")]
    assert ids.index("Look into a rain barrel for the shed") < ids.index("Trial a seed-swap listing site")


def test_notes_puts_rows_carrying_a_note_first(source):
    rows = F.select(source.rows(), "Notes")
    with_note = [bool((r.note or "").strip()) for r in rows]
    assert with_note[0] is True and with_note[-1] is False


# ---------------------------------------------------------------- the Base's own filter language


def test_a_rule_we_cannot_reproduce_is_ignored_and_the_tab_is_flagged(tmp_path):
    vault = _vault(tmp_path)
    base = vault / "Projects" / "Sprints.base"
    base.write_text(
        'views:\n'
        '  - type: table\n'
        '    name: Now\n'
        '    filters:\n'
        '      and:\n'
        '        - file.inFolder("Projects/Sprints")\n'
        '        - done != true\n'
        '        - summary.contains("something we do not handle")\n'
        '    sort:\n'
        '      - property: picked\n'
        '        direction: ASC\n',
        encoding="utf-8",
    )
    src = ob.make(config.Config(vault=str(vault)))
    assert src.flagged_tabs() == {"Now"}
    tab = src.tabs()[0]
    # The rule we could not read is skipped, so the tab shows more rows, never fewer.
    assert len([r for r in src.rows() if tab.filter(r)]) == 8   # everything except the finished row


def test_a_missing_base_file_falls_back_to_the_rules_in_the_specs(tmp_path):
    vault = _vault(tmp_path)
    (vault / "Projects" / "Sprints.base").unlink()
    src = ob.make(config.Config(vault=str(vault)))
    assert [t.name for t in src.tabs()] == F.TAB_ORDER
    assert len([r for r in src.rows() if src.tabs()[0].filter(r)]) == F.counts(src.rows())["Now"]


def test_comparisons_ignore_capitals_and_stray_spaces():
    keep = bf.compile_rule('status != "done"')
    assert keep(TaskRow(id="a", status="Done")) is False
    assert keep(TaskRow(id="a", status="open")) is True
    is_next = bf.compile_rule("next == true")
    assert is_next(TaskRow(id="a", next=True)) is True
    assert is_next(TaskRow(id="a")) is False


def test_an_unknown_rule_raises_rather_than_guessing():
    with pytest.raises(bf.UnknownRule):
        bf.compile_rule("summary.contains('x')")


# ---------------------------------------------------------------- any Base by path


def test_sprints_folder_left_unset_is_derived_from_the_base_file(source, tmp_path):
    """`source` (the module fixture) never sets `sprints_folder`, so this also proves the default
    config used by every other test in this file goes through derivation, not a hard-coded path."""
    assert source.folder == Path(source.cfg.vault) / "Projects" / "Sprints"


def test_an_explicit_sprints_folder_still_wins_over_the_base(tmp_path):
    vault = _vault(tmp_path)
    cfg = config.Config(vault=str(vault), sprints_folder="Projects/Sprints")
    src = ob.make(cfg)
    assert src.folder == Path(vault) / "Projects" / "Sprints"


def test_pointing_base_file_at_a_different_base_follows_its_own_folder(tmp_path):
    """The heart of a second `.base` naming a different folder needs no config edit
    to `sprints_folder` at all -- the source reads whatever folder THAT Base names."""
    vault = tmp_path
    other = vault / "Projects" / "Other"
    other.mkdir(parents=True)
    (other / "one.md").write_text(
        '---\nsummary: "Do the other thing"\nproject: other\ndone: false\nnext: true\n---\n',
        encoding="utf-8",
    )
    base = vault / "Projects" / "Other.base"
    base.write_text(
        "views:\n"
        "  - type: table\n"
        "    name: Now\n"
        "    filters:\n"
        "      and:\n"
        '        - file.inFolder("Projects/Other")\n'
        "        - done != true\n"
        "        - next == true\n",
        encoding="utf-8",
    )
    cfg = config.Config(vault=str(vault), base_file="Projects/Other.base")
    src = ob.make(cfg)
    assert src.folder == other
    assert {r.id for r in src.rows()} == {"one"}
    assert len(src.tabs()) == 1 and src.tabs()[0].name == "Now"


def test_a_base_with_no_infolder_filter_falls_back_and_says_so(tmp_path):
    vault = tmp_path
    (vault / "Projects").mkdir(parents=True)
    base = vault / "Projects" / "Weird.base"
    base.write_text(
        "views:\n  - type: table\n    name: Everything\n    filters:\n      and:\n        - done != true\n",
        encoding="utf-8",
    )
    src = ob.make(config.Config(vault=str(vault), base_file="Projects/Weird.base"))
    # No `Projects/Sprints` folder exists in this fixture either, so the historical-default
    # fallback also comes up empty, and the plain "fix it in pantheon.toml" sentence still shows.
    assert "fix it in pantheon.toml" in src.load_error


# ---------------------------------------------------------------- against the real vault


REAL = config.load()
REAL_FOLDER = Path(REAL.sprints_dir)


@pytest.mark.skipif(not REAL_FOLDER.is_dir(), reason="the real vault is not on this machine")
def test_the_base_file_and_the_specs_agree_on_the_real_vault():
    src = ob.make(REAL)
    rows = src.rows()
    assert rows, "the real Sprints folder had no notes in it"
    spec_counts = F.counts(rows)
    disagreements = []
    for tab in src.tabs():
        if tab.name not in spec_counts:
            continue
        from_base = {r.id for r in rows if tab.filter(r)}
        from_spec = {r.id for r in F.select(rows, tab.name)}
        if from_base != from_spec:
            disagreements.append((tab.name, sorted(from_base ^ from_spec)))
    assert not disagreements, f"Base file and specs disagree: {disagreements}"


def test_a_stale_row_still_saying_fable_reads_as_agent(source):
    """The vault field was renamed `fable` -> `agent` on 2026-09-23. One fixture row keeps the old
    name, as a stale copy would: it still lands on the Agent's plate, and `.fable` still answers."""
    row = next(r for r in source.rows() if r.id == "Write up the tool-hire comparison")
    assert row.agent is True and row.fable is True
    assert "fable" not in row.extra and F.plate(row) is True


def test_agent_wins_when_a_row_carries_both_names(tmp_path):
    import frontmatter
    p = tmp_path / "Both.md"
    p.write_text("---\nsummary: both\nagent: false\nfable: true\n---\n", encoding="utf-8")
    row = ob.row_from_post(p, frontmatter.loads(p.read_text(encoding="utf-8")))
    assert row.agent is False
