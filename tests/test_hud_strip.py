"""The combined-layout budget strip: does it collapse at the same phone
breakpoint as the HUD pane, say when its numbers are stale or missing, and redraw itself on a
timer with no refresher thread of its own?
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from pantheon import config
from pantheon.hud import app as hud_app
from pantheon.hud import strip as hud_strip
from pantheon.models import AgentState, AgentStatus

FIXTURES = Path(__file__).parent / "fixtures" / "hud"
MARKUP = re.compile(r"\[/?[^\]]*\]")


def visible(text: str) -> str:
    """What a terminal actually shows: colour markup takes up no columns."""
    return MARKUP.sub("", text)


def widget_text(widget) -> str:
    content = widget.content
    return getattr(content, "plain", None) or str(content)


def _picture(age_minutes: int = 0) -> dict:
    with open(FIXTURES / "hud.json", "r", encoding="utf-8") as fh:
        picture = json.load(fh)
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    picture["fetched_at"] = stamp
    picture["claude"]["fetched_at"] = stamp
    picture["codex"]["fetched_at"] = stamp
    return picture


@pytest.fixture()
def make_cfg(tmp_path):
    def _make(glyphs: str = "unicode", picture: dict | None = None, write: bool = True):
        cfg = config.Config(state_dir=str(tmp_path / glyphs / "state"), appearance={"glyphs": glyphs})
        if write:
            target = Path(cfg.hud_file)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(picture if picture is not None else _picture()), encoding="utf-8")
        return cfg

    return _make


# ---- render_strip -----------------------------------------------------------

def test_two_lines_per_provider_at_120(make_cfg):
    cfg = make_cfg()
    lines = hud_strip.render_strip(_picture(), cfg, 120)
    shown = [visible(line) for line in lines]

    claude = [i for i, line in enumerate(shown) if line.startswith("Claude")]
    codex = [i for i, line in enumerate(shown) if line.startswith("Codex")]
    assert len(claude) == 1 and len(codex) == 1
    for start in (claude[0], codex[0]):
        first, second = shown[start], shown[start + 1]
        assert "5h" in first and "week" in first
        assert "resets" in second and "today" in second
    # 2026-09-26: an unknown burn is left out (the fixture's Codex has none), never `burn ?`.
    assert "burn" in shown[claude[0] + 1] and "burn ?" not in "\n".join(shown)

    # no frame of its own: no title line, no "read at" line (the HUD pane owns those)
    assert not any(line.startswith("BUDGET") for line in shown)
    assert not any(line.startswith("read at") for line in shown)


def test_one_line_per_provider_at_60(make_cfg):
    cfg = make_cfg()
    lines = hud_strip.render_strip(_picture(), cfg, 60)
    shown = [visible(line) for line in lines]
    for line in shown:
        assert len(line) <= 60, line

    claude = [i for i, line in enumerate(shown) if line.startswith("Claude")]
    codex = [i for i, line in enumerate(shown) if line.startswith("Codex")]
    assert len(claude) == 1 and len(codex) == 1


def test_stale_picture_gets_the_minutes_old_sentence(make_cfg):
    cfg = make_cfg()
    old = _picture(age_minutes=20)
    lines = hud_strip.render_strip(old, cfg, 120)
    shown = [visible(line) for line in lines]
    assert any("20 minutes old" in line and "usage window (F3)" in line for line in shown)


def test_missing_file_gets_the_open_the_usage_window_sentence(make_cfg):
    cfg = make_cfg(write=False)
    picture = hud_strip._read_picture(cfg.hud_file)
    assert picture == {}
    lines = hud_strip.render_strip(picture, cfg, 120)
    shown = [visible(line) for line in lines]
    assert any("no usage file yet" in line and "usage window (F3)" in line for line in shown)


def test_bad_file_does_not_raise(make_cfg):
    cfg = make_cfg(write=False)
    target = Path(cfg.hud_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    picture = hud_strip._read_picture(cfg.hud_file)
    assert picture == {}
    lines = hud_strip.render_strip(picture, cfg, 120)
    assert any("no usage file yet" in visible(line) for line in lines)


def test_height_hint_matches_render_strip_line_count(make_cfg):
    cfg = make_cfg()
    cases = ((_picture(), 120), (_picture(), 60), (_picture(age_minutes=20), 120), ({}, 60))
    for picture, width in cases:
        assert hud_strip.height_hint(picture, width) == len(
            hud_strip.render_strip(picture, cfg, width)
        )


# ---- the widget itself: a row of cards -------------------

class _Host(App):
    """Mounts one `HudStrip`, optionally handing it an `agents_source` (the deck's contract:
    `agents_source=lambda: self.sup.rows`)."""

    def __init__(self, cfg, agents_source=None) -> None:
        super().__init__()
        self._cfg = cfg
        self._agents_source = agents_source

    def compose(self) -> ComposeResult:
        yield hud_strip.HudStrip(self._cfg, agents_source=self._agents_source)


def _mount(cfg, size=(200, 12), agents_source=None):
    async def _go():
        app = _Host(cfg, agents_source=agents_source)
        async with app.run_test(size=size):
            strip = app.query_one(hud_strip.HudStrip)
            cards = list(strip.query(hud_app.ProviderCard))
            titles = [card.border_title for card in cards]
            gauges = {}
            for card in cards:
                gauges[card.border_title] = [
                    widget_text(w) for w in card.query(".hud-gaugeline")
                ]
            needs_you = strip.query(hud_app.NeedsYouCard)
            needs_you_text = (
                widget_text(needs_you.first().query_one(Static)) if needs_you else None
            )
            needs_you_display = needs_you.first().display if needs_you else None
            picture = strip.picture
        return titles, gauges, needs_you_text, needs_you_display, picture

    return asyncio.run(_go())


def test_strip_builds_one_card_per_provider_with_percents(make_cfg):
    cfg = make_cfg(picture=_picture(age_minutes=0))
    titles, gauges, _needs_text, _needs_display, picture = _mount(cfg)

    assert any("CLAUDE" in (t or "") for t in titles)
    assert any("CODEX" in (t or "") for t in titles)

    claude_title = next(t for t in titles if "CLAUDE" in t)
    codex_title = next(t for t in titles if "CODEX" in t)
    claude_lines = "\n".join(gauges[claude_title])
    codex_lines = "\n".join(gauges[codex_title])
    assert "5h" in claude_lines and "42%" in claude_lines
    assert "week" in claude_lines and "61%" in claude_lines
    assert "5h" in codex_lines and "81%" in codex_lines

    assert picture["claude"]["five_hour_pct"] == 42.0  # HudStrip.picture is the last read file


def test_needs_you_card_lists_who_needs_the_user_and_who_failed(make_cfg):
    cfg = make_cfg(picture=_picture(age_minutes=0))
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = [
        AgentState(
            session_id="a1", provider="claude", status=AgentStatus.BLOCKED_PERMISSION,
            project="loom-os", last_action="Bash: git status", last_event_ts=now_iso,
        ),
        AgentState(
            session_id="a2", provider="codex", status=AgentStatus.FAILED,
            project="Plumb", last_action="codex exec exit code 1", last_event_ts=now_iso,
        ),
        AgentState(
            session_id="a3", provider="claude", status=AgentStatus.WORKING,
            project="hiking_log_v2", last_action="Edit", last_event_ts=now_iso,
        ),
    ]
    _titles, _gauges, needs_text, needs_display, _pic = _mount(
        cfg, size=(200, 12), agents_source=lambda: rows
    )
    assert needs_display is True  # 200 columns >= NEEDS_YOU_WIDTH_BREAKPOINT
    assert "loom-os" in needs_text and "Bash: git status" in needs_text
    assert "Plumb" in needs_text
    assert "hiking_log_v2" not in needs_text  # WORKING: does not need the user, did not fail


def test_needs_you_card_says_nothing_needs_you_when_quiet(make_cfg):
    cfg = make_cfg(picture=_picture(age_minutes=0))
    _titles, _gauges, needs_text, _display, _pic = _mount(cfg, agents_source=lambda: [])
    assert "nothing needs you" in needs_text


def test_needs_you_card_hidden_under_150_columns(make_cfg):
    cfg = make_cfg(picture=_picture(age_minutes=0))
    _titles, _gauges, _text, needs_display, _pic = _mount(cfg, size=(140, 12))
    assert needs_display is False


def test_needs_you_card_shown_at_150_columns(make_cfg):
    cfg = make_cfg(picture=_picture(age_minutes=0))
    _titles, _gauges, _text, needs_display, _pic = _mount(cfg, size=(150, 12))
    assert needs_display is True


def test_agents_source_that_raises_does_not_crash_the_strip(make_cfg):
    cfg = make_cfg(picture=_picture(age_minutes=0))

    def _boom():
        raise RuntimeError("no tmux")

    titles, _gauges, needs_text, _display, _pic = _mount(cfg, agents_source=_boom)
    assert any("CLAUDE" in (t or "") for t in titles)  # the rest of the strip still draws
    assert "nothing needs you" in needs_text


def test_missing_file_mounts_with_no_provider_cards_but_does_not_crash(make_cfg):
    cfg = make_cfg(write=False)
    titles, _gauges, needs_text, _display, picture = _mount(cfg)
    assert titles == []
    assert picture == {}
    assert needs_text == "nothing needs you"


def test_bad_file_does_not_crash_the_widget(make_cfg):
    # `_read_picture` already fails open to `{}` on bad JSON, same as `render_strip`'s own
    # `test_bad_file_does_not_raise` above -- the widget just draws the "no usage file yet"
    # note (`#hud-strip-error` is a further belt-and-suspenders path for a genuinely unexpected
    # exception while composing, not exercised by a merely-unreadable file).
    cfg = make_cfg(write=False)
    target = Path(cfg.hud_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")

    titles, _gauges, needs_text, _display, picture = _mount(cfg)
    assert titles == []
    assert picture == {}
    assert needs_text == "nothing needs you"


# ---- refresh_needs_you ---------------------------

def test_refresh_needs_you_repaints_without_recompose(make_cfg):
    cfg = make_cfg(picture=_picture(age_minutes=0))
    rows_box = {"rows": []}

    async def _go():
        app = _Host(cfg, agents_source=lambda: rows_box["rows"])
        async with app.run_test(size=(200, 12)):
            strip = app.query_one(hud_strip.HudStrip)
            needs_before = widget_text(strip.query_one(hud_app.NeedsYouCard).query_one(Static))
            recomposed = {"called": False}
            orig_recompose = strip.recompose

            async def _spy():
                recomposed["called"] = True
                return await orig_recompose()

            strip.recompose = _spy
            rows_box["rows"] = [
                AgentState(
                    session_id="a1", provider="claude", status=AgentStatus.BLOCKED_PERMISSION,
                    project="loom-os", last_action="Bash: git status",
                    last_event_ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                )
            ]
            strip.refresh_needs_you()
            needs_after = widget_text(strip.query_one(hud_app.NeedsYouCard).query_one(Static))
            return needs_before, needs_after, recomposed["called"]

    needs_before, needs_after, recomposed_called = asyncio.run(_go())
    assert "nothing needs you" in needs_before
    assert "loom-os" in needs_after and "Bash: git status" in needs_after
    assert recomposed_called is False   # no file read, no recompose -- just the one card repainted


def test_refresh_needs_you_survives_a_missing_card(make_cfg):
    cfg = make_cfg(write=False)

    async def _go():
        app = _Host(cfg, agents_source=lambda: [])
        async with app.run_test(size=(120, 12)):
            strip = app.query_one(hud_strip.HudStrip)
            strip.refresh_needs_you()   # must not raise even when nothing is amiss

    asyncio.run(_go())


def test_ascii_glyphs_produce_pure_ascii_gauges_and_trend(make_cfg):
    cfg = make_cfg("ascii", picture=_picture(age_minutes=0))
    titles, gauges, needs_text, _display, _pic = _mount(cfg)
    claude_title = next(t for t in titles if "CLAUDE" in t)
    joined = "\n".join(gauges[claude_title]) + "\n" + (needs_text or "")
    assert all(ord(c) < 128 for c in joined), [c for c in joined if ord(c) >= 128][:5]
    assert "#" in "\n".join(gauges[claude_title])  # the ASCII bar-fill character



def test_budget_lines_need_you_only_while_that_provider_is_running():
    from pantheon.hud.strip import attention_for_active
    from pantheon.models import AgentState, AgentStatus
    lines = ["Codex: 87% of this week's budget is used", "Claude: 91% of this 5-hour budget is used",
             "This session's memory is 85% full - it will summarise itself soon"]
    idle_codex = AgentState(session_id="x", provider="codex", status=AgentStatus.DONE)
    busy_claude = AgentState(session_id="y", provider="claude", status=AgentStatus.WORKING)
    kept = attention_for_active(lines, [idle_codex, busy_claude])
    assert kept == [lines[1], lines[2]]          # Codex idle -> its line drops; memory passes through
    assert attention_for_active(lines, []) == [lines[2]]
