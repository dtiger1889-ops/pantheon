"""the `## Open threads` parser (`pantheon/tasks/checkpoint_threads.py`).

Pure text in, structured `Thread`s out -- no file I/O in this module, so every case here hands it
a string directly. `tests/fixtures/checkpoints/*/CHECKPOINT.md` holds three MADE-UP fixture files
(never this repo's own CHECKPOINT.md, per the build brief) used by the read-from-disk cases at the
bottom, which double as a first pass at `checkpoint_source.py` (the adapter that reads them).
"""
from __future__ import annotations

from pathlib import Path

from pantheon import config
from pantheon.tasks import checkpoint_source
from pantheon.tasks.checkpoint_threads import Thread, parse_open_threads

FIXTURES = Path(__file__).parent / "fixtures" / "checkpoints"

TAGGED = """\
## Open threads
Tags: `[the user]` = your hands; `[agent]` = any model session -- `[gates: go]` waits on your word.

- **Buy the replacement widget [the user] [low].** The old one cracked.
- **Ship the render pipeline [agent] [high] [gates: go].** Blocked on the go.
- **Bare thread with no tags.** Falls back to unknown/normal.
- Plain bullet, no bold lead either. Still a valid thread.
- **Two tags at once [agent] [low].** No gate here.

## Next step
Not part of the section.
"""


# ---------------------------------------------------------------- the basic shape


def test_a_tagged_section_parses_to_one_thread_per_bullet():
    threads = parse_open_threads(TAGGED)
    assert len(threads) == 5


def test_the_user_and_low_tag_map_to_owner_and_priority():
    threads = parse_open_threads(TAGGED)
    t = threads[0]
    assert t.title == "Buy the replacement widget"
    assert t.owner == "the user"
    assert t.priority == "low"
    assert t.gates is None
    assert "old one cracked" in t.detail


def test_agent_high_and_gates_all_parse_off_one_bullet():
    threads = parse_open_threads(TAGGED)
    t = threads[1]
    assert t.title == "Ship the render pipeline"
    assert t.owner == "agent"
    assert t.priority == "high"
    assert t.gates == "go"


def test_a_bolded_bullet_with_no_bracket_tags_falls_back_to_unknown_and_normal():
    threads = parse_open_threads(TAGGED)
    t = threads[2]
    assert t.title == "Bare thread with no tags"
    assert t.owner == "unknown"
    assert t.priority == "normal"
    assert t.gates is None


def test_a_plain_bullet_with_no_bold_lead_is_still_a_valid_thread():
    threads = parse_open_threads(TAGGED)
    t = threads[3]
    assert t.title == "Plain bullet, no bold lead either"
    assert t.owner == "unknown"
    assert t.priority == "normal"


def test_two_tags_at_once_both_apply():
    threads = parse_open_threads(TAGGED)
    t = threads[4]
    assert t.owner == "agent"
    assert t.priority == "low"
    assert t.gates is None


def test_the_tags_legend_line_is_not_itself_a_thread():
    titles = [t.title for t in parse_open_threads(TAGGED)]
    assert not any("Tags:" in title or "waits on your word" in title for title in titles)


# ---------------------------------------------------------------- section boundaries


def test_a_bullet_after_the_next_heading_is_not_included():
    text = TAGGED + "\n- **Should never parse [the user] [high].** Past the boundary.\n"
    # the extra bullet sits under "## Next step", already past the section in TAGGED
    threads = parse_open_threads(text)
    assert len(threads) == 5
    assert all("Should never parse" != t.title for t in threads)


def test_a_continuation_paragraph_lands_in_detail_not_as_a_new_row():
    text = (
        "## Open threads\n"
        "- **Ship the render pipeline [agent] [high] [gates: go].** Blocked on the go.\n"
        "  Continuation line at the same indent as a wrapped paragraph -- this belongs to the\n"
        "  render pipeline thread's detail, not a new row of its own.\n"
        "- **Second thread [the user] [low].** Unrelated.\n"
    )
    threads = parse_open_threads(text)
    assert len(threads) == 2
    assert "Continuation line" in threads[0].detail
    assert threads[1].title == "Second thread"


def test_a_blank_line_between_bullets_does_not_start_a_new_thread():
    text = (
        "## Open threads\n"
        "- **First [the user] [low].** One.\n"
        "\n"
        "- **Second [agent] [high].** Two.\n"
    )
    threads = parse_open_threads(text)
    assert len(threads) == 2
    assert threads[0].title == "First"
    assert threads[1].title == "Second"


def test_the_heading_match_is_case_insensitive():
    text = "## open THREADS\n- **Case insensitive [the user] [low].** Still found.\n"
    threads = parse_open_threads(text)
    assert len(threads) == 1
    assert threads[0].title == "Case insensitive"


def test_no_open_threads_section_returns_an_empty_list():
    text = "# fixture_project\n\n## Status\nNothing relevant here.\n\n## Next step\nDone.\n"
    assert parse_open_threads(text) == []


def test_an_empty_string_returns_an_empty_list():
    assert parse_open_threads("") == []


def test_an_open_threads_section_with_no_bullets_returns_an_empty_list():
    text = "## Open threads\nNothing to do right now.\n\n## Next step\nDone.\n"
    assert parse_open_threads(text) == []


# ---------------------------------------------------------------- fixture files on disk


def test_the_with_threads_fixture_parses_to_five_threads():
    text = (FIXTURES / "project_with_threads" / "CHECKPOINT.md").read_text(encoding="utf-8")
    threads = parse_open_threads(text)
    assert len(threads) == 5
    assert [t.title for t in threads] == [
        "Buy the replacement widget",
        "Ship the render pipeline",
        "Bare thread with no tags",
        "Plain bullet, no bold lead either",
        "Two tags at once",
    ]
    assert "Continuation line" in threads[1].detail
    assert not any(t.title == "Should never parse as a thread" for t in threads)


def test_the_no_threads_fixture_parses_to_an_empty_list():
    text = (FIXTURES / "project_no_threads" / "CHECKPOINT.md").read_text(encoding="utf-8")
    assert parse_open_threads(text) == []


def test_the_bare_bullets_fixture_parses_with_unknown_owner_and_normal_priority():
    text = (FIXTURES / "project_bare_bullets" / "CHECKPOINT.md").read_text(encoding="utf-8")
    threads = parse_open_threads(text)
    assert len(threads) == 3
    assert all(t.owner == "unknown" and t.priority == "normal" for t in threads)
    assert threads[0].title == "Water the office plants"


# ---------------------------------------------------------------- the adapter (checkpoint_source)


def _cfg() -> config.Config:
    return config.Config(projects_root=str(FIXTURES))


def test_the_adapter_reads_a_fixture_projects_checkpoint_into_rows():
    source = checkpoint_source.make(_cfg(), "project_with_threads")
    rows = source.rows()
    assert len(rows) == 5
    assert source.name == "checkpoint"
    assert source.kind == "CHECKPOINT: project_with_threads"
    by_summary = {r.summary: r for r in rows}
    assert by_summary["Buy the replacement widget"].tier == "low"
    assert by_summary["Buy the replacement widget"].extra["owner"] == "the user"
    assert by_summary["Ship the render pipeline"].status == "go"


def test_the_adapter_says_so_when_a_project_has_no_open_threads_section():
    source = checkpoint_source.make(_cfg(), "project_no_threads")
    assert source.rows() == []
    assert "Open threads" in source.notice


def test_the_adapter_says_so_when_a_project_has_no_checkpoint_at_all():
    source = checkpoint_source.make(_cfg(), "no_such_project")
    assert source.rows() == []
    assert "no CHECKPOINT.md" in source.notice


def test_the_adapter_tabs_split_by_owner_and_gate():
    source = checkpoint_source.make(_cfg(), "project_with_threads")
    rows = source.rows()
    tabs_by_name = {t.name: t for t in source.tabs()}
    needs_the_user = [r for r in rows if tabs_by_name["Needs the user"].filter(r)]
    agent = [r for r in rows if tabs_by_name["Agent"].filter(r)]
    gated = [r for r in rows if tabs_by_name["Gated"].filter(r)]
    assert {r.summary for r in needs_the_user} == {"Buy the replacement widget"}
    assert {r.summary for r in agent} == {"Ship the render pipeline", "Two tags at once"}
    assert {r.summary for r in gated} == {"Ship the render pipeline"}
    assert len(rows) == 5


def test_the_adapter_never_writes_the_checkpoint_file():
    path = FIXTURES / "project_with_threads" / "CHECKPOINT.md"
    before = path.read_bytes()
    source = checkpoint_source.make(_cfg(), "project_with_threads")
    source.rows()
    source.refresh()
    source.open_for_edit(source.rows()[0])
    after = path.read_bytes()
    assert before == after
