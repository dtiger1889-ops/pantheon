"""`pantheon/session_view/grid.py` -- the wall of conversations.

Three things this file exists to hold honest, all three of them the user's own rejection criteria
for rather than internal tidiness:

1. **How many cards fit is a width decision.** One column on a narrow deck, two from 160 content
   columns, three from 240, and never more than two rows of them -- past that the cards get too
   short to read as a chat, which is the "still looks like a terminal table" failure.
2. **Only the cards ON SCREEN are re-read.** A session below the fold, or hidden behind an
   expanded card, must cost nothing per tick. Counted here with a monkeypatched parser: the
   number of parse calls is the number of visible cards, not the number of sessions.
3. **A tick stays fast when several sessions are busy.** Ten sessions with a 5 MB transcript
   each, one tick's worth of parser work, under 250 ms -- the acceptance number in the spec.
   The first read of a 5 MB file is not what a tick does; a tick calls `update`, which reads
   only the bytes appended since last time, so that is what is timed.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from textual.app import App

from pantheon.session_view import grid as grid_mod
from pantheon.session_view import models as m
from pantheon.session_view import transcript as transcript_mod

CWD = "C:/Home/x/Documents/Projects/hiking_log_v2"


def _entry(session_id: str, transcript_path=None, needs_human=False, project="hiking_log_v2"):
    return m.SessionEntry(
        session_id=session_id, group=m.LIVE, project=project, cwd=CWD,
        title=f"session {session_id}", status_text="working", needs_human=needs_human,
        window_index=3, tmux_session="pantheon", transcript_path=transcript_path,
    )


# ---------------------------------------------------------------- 1. columns per width


def test_columns_per_width():
    """The tiers are measured against the GRID's own content width, never the terminal's -- the
    deck spends 30 columns on the SESSIONS sidebar before the wall sees any."""
    assert grid_mod.columns_for(0) == 1
    assert grid_mod.columns_for(120) == 1
    assert grid_mod.columns_for(grid_mod.COLS_2_AT - 1) == 1
    assert grid_mod.columns_for(grid_mod.COLS_2_AT) == 2
    assert grid_mod.columns_for(grid_mod.COLS_3_AT - 1) == 2
    assert grid_mod.columns_for(grid_mod.COLS_3_AT) == 3
    assert grid_mod.columns_for(400) == 3


class _WallApp(App):
    """The wall on its own, at whatever terminal size the test asks for -- no deck, no sidebar,
    no BUDGET strip, so the width the wall sees IS the width under test."""

    def __init__(self, entries):
        super().__init__()
        self.wall = grid_mod.ConversationGrid(entries_source=lambda: entries)

    def compose(self):
        yield self.wall


@pytest.mark.parametrize("width,expected", [(100, 1 * grid_mod.MAX_ROWS),
                                            (170, 2 * grid_mod.MAX_ROWS),
                                            (250, 3 * grid_mod.MAX_ROWS)])
def test_the_wall_mounts_columns_times_rows_cards_at_each_width(tmp_path, width, expected):
    """Eight sessions, three widths: the wall never mounts more than `columns x MAX_ROWS` cards.
    The rest are not lost -- they are one arrow key away in the sidebar, which lists them all."""
    entries = [_entry(f"s{i}", _write_transcript(tmp_path / f"s{i}.jsonl")) for i in range(8)]

    async def _run():
        app = _WallApp(entries)
        async with app.run_test(size=(width, 40)) as pilot:
            await pilot.pause()
            assert grid_mod.columns_for(app.wall.size.width) == grid_mod.columns_for(width)
            assert len(app.wall.tiles()) == expected

    asyncio.run(_run())


def test_needs_you_sessions_come_first():
    entries = [_entry("calm1"), _entry("loud", needs_human=True), _entry("calm2")]
    assert [e.session_id for e in grid_mod.order_entries(entries)][0] == "loud"


# ---------------------------------------------------------------- 2. only visible tiles parse


def _write_transcript(path: Path, lines: int = 2) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u1", "sessionId": path.stem, "cwd": CWD,
                             "message": {"role": "user", "content": "go"}}) + "\n")
        for i in range(lines):
            fh.write(json.dumps({
                "type": "assistant", "uuid": f"a{i}", "sessionId": path.stem,
                "message": {"role": "assistant", "model": "claude-opus-5",
                            "content": [{"type": "text", "text": f"line {i}"}]},
            }) + "\n")
    return str(path)


def test_only_the_visible_tiles_are_re_read(tmp_path, monkeypatch):
    """Eight sessions, a wall two columns wide: a tick parses the four cards on screen and
    nothing else. Counted at the parser, which is the only place the work actually happens."""
    entries = [_entry(f"s{i}", _write_transcript(tmp_path / f"s{i}.jsonl")) for i in range(8)]
    parsed: list[str] = []

    def _counting_load(path):
        parsed.append(str(path))
        return m.Conversation(session_id=Path(path).stem, path=str(path))

    monkeypatch.setattr(transcript_mod, "load", _counting_load)

    wall = grid_mod.ConversationGrid(entries_source=lambda: entries)
    wall.entries = grid_mod.order_entries(entries)
    wall._columns = 2
    wall._laid_out = wall.entries[: 2 * grid_mod.MAX_ROWS]

    visible = wall.visible_entries()
    assert len(visible) == 4
    grid_mod.parse_entries(visible)
    assert len(parsed) == 4
    assert parsed == [str(tmp_path / f"s{i}.jsonl") for i in range(4)]


def test_an_expanded_tile_is_the_only_thing_a_tick_reads(tmp_path, monkeypatch):
    """Expanding one session takes every other card off the wall, so a tick reads exactly one
    transcript -- the busiest case (a long chat, followed live) is also the cheapest."""
    entries = [_entry(f"s{i}", _write_transcript(tmp_path / f"s{i}.jsonl")) for i in range(4)]
    parsed: list[str] = []
    monkeypatch.setattr(transcript_mod, "load",
                        lambda path: (parsed.append(str(path))
                                      or m.Conversation(session_id="x", path=str(path))))

    wall = grid_mod.ConversationGrid(entries_source=lambda: entries)
    wall.entries = grid_mod.order_entries(entries)
    wall._laid_out = list(wall.entries)
    wall._expanded = entries[2]

    visible = wall.visible_entries()
    assert [e.session_id for e in visible] == ["s2"]
    grid_mod.parse_entries(visible)
    assert parsed == [str(tmp_path / "s2.jsonl")]


def test_a_session_with_no_transcript_costs_no_parser_call(tmp_path, monkeypatch):
    """A Codex job before its rollout file appears: the card says so in one line, and the tick
    never reaches a parser at all."""
    calls: list[str] = []
    monkeypatch.setattr(transcript_mod, "load", lambda path: calls.append(str(path)))
    assert grid_mod.parse_entries([_entry("nofile", None)]) == {}
    assert calls == []


def test_a_broken_transcript_keeps_the_last_good_conversation(tmp_path, monkeypatch):
    """A transcript that has been truncated or half-written mid-tick must never take the deck
    down; the card keeps showing what it last read."""
    entry = _entry("s1", _write_transcript(tmp_path / "s1.jsonl"))
    good = m.Conversation(session_id="s1", path=entry.transcript_path)

    def _boom(_conv):
        raise ValueError("half a line")

    monkeypatch.setattr(transcript_mod, "update", _boom)
    assert grid_mod.read_conversation(entry, good) is good


def test_a_codex_entry_goes_to_the_codex_parser():
    from pantheon.session_view import codex_transcript as codex_mod

    claude = _entry("s1", "x.jsonl")
    codex = m.SessionEntry(session_id="s2", group=m.LIVE, project="p", cwd=CWD, title="t",
                           provider="codex", transcript_path="rollout.jsonl")
    assert grid_mod.parser_for(claude) is transcript_mod
    assert grid_mod.parser_for(codex) is codex_mod


# ---------------------------------------------------------------- 3. the tick stays fast


def _big_transcript(path: Path, target_bytes: int) -> str:
    """A synthetic Claude transcript of roughly `target_bytes`, built from the record shapes the
    parser actually reads (prose, a tool call and its result, a thinking block) -- never a copy
    of one of the user's real transcripts."""
    filler = "x" * 400
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u0", "sessionId": path.stem, "cwd": CWD,
                             "message": {"role": "user", "content": "go"}}) + "\n")
        written = 0
        i = 0
        while written < target_bytes:
            records = [
                {"type": "assistant", "uuid": f"a{i}", "sessionId": path.stem,
                 "message": {"role": "assistant", "model": "claude-opus-5",
                             "usage": {"input_tokens": 10, "output_tokens": 5},
                             "content": [{"type": "thinking", "thinking": filler},
                                         {"type": "text", "text": filler},
                                         {"type": "tool_use", "id": f"toolu_{i}", "name": "Read",
                                          "input": {"file_path": "C:/x/CHECKPOINT.md"}}]}},
                {"type": "user", "uuid": f"r{i}", "sessionId": path.stem,
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}",
                                          "content": filler}]}},
            ]
            for record in records:
                line = json.dumps(record) + "\n"
                fh.write(line)
                written += len(line)
            i += 1
    return str(path)


def test_ten_busy_sessions_stay_under_the_tick_budget(tmp_path):
    """Acceptance 4: ten live sessions, a 5 MB transcript each, one tick's parser work under
    250 ms. A tick is an `update()` per visible card -- append-only, so it reads the bytes that
    arrived since last time, not the 5 MB again. Each session here gets a fresh turn appended
    between the first read and the timed one, so the timing covers real work, not a no-op."""
    entries = []
    for i in range(10):
        path = tmp_path / f"big{i}.jsonl"
        entries.append(_entry(f"s{i}", _big_transcript(path, 5 * 1024 * 1024)))

    previous = grid_mod.parse_entries(entries)
    assert len(previous) == 10
    for conv in previous.values():
        assert conv.items, "the synthetic transcript should have parsed into items"

    for i in range(10):
        with open(tmp_path / f"big{i}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "assistant", "uuid": f"new{i}", "sessionId": f"big{i}",
                "message": {"role": "assistant", "model": "claude-opus-5",
                            "content": [{"type": "text", "text": "one more turn"}]},
            }) + "\n")

    started = time.perf_counter()
    parsed = grid_mod.parse_entries(entries, previous)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert len(parsed) == 10
    assert elapsed_ms < 250, f"one tick's parser work took {elapsed_ms:.0f} ms, budget is 250 ms"
