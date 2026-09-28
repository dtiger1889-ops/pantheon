"""The supervisor screen, driven headlessly by Textual's own test pilot.

No tmux server is touched: the window list is injected, so these tests never see (or disturb) a
real tmux session. What they check is what the user actually reads -- the header wording, the `!!`
marker and the words `blocked - permission` on the row that needs him, the `where` cell that says
which tmux window an agent sits in (or that it is not in tmux at all), and the fact that each of
the three layouts fits its screen without sideways scrolling: 65 columns on the phone, 80 in a
narrow window, 120 at the desk.
"""
import asyncio
import dataclasses
import html
import json
import re
from datetime import datetime, timezone

import pytest
from textual.widgets import DataTable, Static

from pantheon import config as config_mod
from pantheon import glyphs as glyphs_mod
from pantheon.models import AgentState, AgentStatus, TmuxWindow, utcnow_iso
from pantheon.supervisor import pane as pane_mod
from pantheon.supervisor.app import (
    NARROW_COLUMNS,
    PHONE_COLUMNS,
    SUPER_WIDE_COLUMNS,
    WIDE_COLUMNS,
    SupervisorApp,
)
from pantheon.supervisor.pane import NOT_IN_TMUX_JUMP, NOT_IN_TMUX_KILL, state_cell

CWD = "C:/Home/x/Documents/Projects/hiking_log_v2"
NBSP = "\u00a0"
MIDDOT = "\u00b7"

FIXTURE = [
    {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
     "session_id": "abc12345deadbeef", "cwd": CWD, "tmux_pane": "%3"},
    {"ts": utcnow_iso(), "source": "claude", "event": "Notification",
     "session_id": "abc12345deadbeef", "cwd": CWD, "tmux_pane": "%3",
     "notification_type": "permission_prompt",
     "message": "Claude needs your permission to use Bash"},
]

# The same agent, but sitting in a real pantheon window -- so `where` has something to say.
PANTHEON_WINDOW = TmuxWindow(
    index=3, name="hiking_log_v2", command="node", path=CWD, pane_id="%3", session="pantheon"
)

# `FIXTURE`'s timestamps are stamped the moment this module is imported (real wall clock, above).
# `pantheon.supervisor.state._is_present` treats a row with a recorded `tmux_pane` but no live
# window (every test here injects `windows=` by default) as dead once its age passes
# `DEAD_PANE_SECONDS` (180s) -- so a slow/loaded test run (parallel suites, a loaded box) can let
# real time drift past that between import and the moment a given test actually executes, and the
# fixture agent silently vanishes from the table. Freeze the one clock this
# pane reads (`pantheon.supervisor.pane`'s `datetime.now`) to the instant `FIXTURE` was stamped, the
# same `_freeze_clock` pattern `test_snapshots.py` uses -- so age is always ~0 regardless of how
# long the suite takes.
FROZEN_NOW = datetime.now(timezone.utc)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz is not None else FROZEN_NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(pane_mod, "datetime", _FrozenDateTime)


def make_config(tmp_path, fixture=None):
    cfg = config_mod.Config(state_dir=str(tmp_path))
    events_file = cfg.events_file
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(events_file, "w", encoding="utf-8") as fh:
        for d in fixture or FIXTURE:
            fh.write(json.dumps(d) + "\n")
    return dataclasses.replace(cfg, projects_root="C:/Home/x/Documents/Projects")


def screen_text(svg: str) -> str:
    """The words a person would read off the rendered screen, with the SVG markup taken away."""
    runs = re.findall(r"<text[^>]*>(.*?)</text>", svg, flags=re.S)
    return html.unescape("".join(runs)).replace(NBSP, " ")


def styles_of(static: Static) -> str:
    """Every colour the footer line is painted with, as one searchable string."""
    content = static.content
    return " ".join(str(getattr(span, "style", "")) for span in getattr(content, "spans", []))


def run_at(tmp_path, width, height=24, windows=(), fixture=None):
    """Start the deck at a given terminal size and hand back what it drew."""
    app = SupervisorApp(cfg=make_config(tmp_path, fixture), window_source=lambda: list(windows))
    captured = {}

    async def drive():
        async with app.run_test(size=(width, height)) as pilot:
            await pilot.pause()
            table = app.query_one("#agents", DataTable)
            captured["header"] = str(app.query_one("#header", Static).content)
            captured["status"] = str(app.query_one("#status", Static).content)
            captured["columns"] = [str(c.label) for c in table.columns.values()]
            captured["cells"] = [c.plain if hasattr(c, "plain") else str(c)
                                 for c in table.get_row_at(0)]
            captured["screen"] = screen_text(app.export_screenshot())
            captured["rows"] = list(app.pane.rows)
            captured["table_width"] = table.virtual_size.width
            captured["viewport_width"] = table.container_size.width

    asyncio.run(drive())
    return captured


def test_header_counts_in_plain_words(tmp_path):
    drawn = run_at(tmp_path, 80)
    assert drawn["header"] == f"PANTHEON {MIDDOT} agents  0 working {MIDDOT} 1 need you"
    assert "pending approvals" not in drawn["header"]      # design system R4
    assert "(no tmux)" in drawn["status"]
    assert "parse errors" not in drawn["status"]           # silent when the log is clean


def test_phone_layout_fits_65_columns(tmp_path):
    # Phone tier: one full-width column, each session on two lines -- glyph + title,
    # then the dim `status · project · where · age` -- clipped, never wrapped, no sideways scroll.
    drawn = run_at(tmp_path, 65)
    assert drawn["columns"] == ["session"]
    first, second = drawn["cells"][0].splitlines()
    assert first.startswith("!! ")                       # the permission marker stays on line 1
    assert second.strip().startswith("needs approval · hiking_log_v2 · desktop")
    assert "needs approval · hiking_log_v2" in drawn["screen"]
    assert drawn["table_width"] <= drawn["viewport_width"]


def test_phone_rows_clip_each_line_at_46_columns(tmp_path):
    drawn = run_at(tmp_path, 46, height=30)
    first, second = drawn["cells"][0].splitlines()
    width = 46 - 4
    assert len(first) <= width and len(second) <= width
    assert second.strip().startswith("needs approval · hiking_log_v2")
    assert drawn["table_width"] <= drawn["viewport_width"]


def test_blocked_row_shows_the_marker_and_the_words_at_80_columns(tmp_path):
    drawn = run_at(tmp_path, 80)
    assert len(drawn["columns"]) == len(NARROW_COLUMNS)
    assert drawn["columns"] == ["state", "project", "where", "last action", "age"]
    state_cell = drawn["cells"][0]
    assert state_cell.startswith("!!")
    assert "blocked - permission" in state_cell
    assert drawn["cells"][1] == "hiking_log_v2"
    # And it really reached the screen, not just the widget.
    assert "!! blocked - permission" in drawn["screen"]
    assert "1 need you" in drawn["screen"]
    # No sideways scrolling on the phone.
    assert drawn["table_width"] <= drawn["viewport_width"]


# ---------------------------------------------------------------- [appearance] icons


def test_state_cell_uses_the_plain_glyph_with_icons_off():
    row = AgentState(session_id="s1", provider="claude", status=AgentStatus.WORKING)
    plain = glyphs_mod.icon_table("unicode", False)
    assert state_cell(row, plain) == f"{glyphs_mod.UNICODE['working']} working"


def test_state_cell_uses_the_nerd_glyph_with_icons_on():
    # The word is always there either way -- only the leading mark changes.
    row = AgentState(session_id="s1", provider="claude", status=AgentStatus.WORKING)
    with_icons = glyphs_mod.icon_table("unicode", True)
    cell = state_cell(row, with_icons)
    assert cell == f"{glyphs_mod.NERD['working']} working"
    assert cell.endswith(" working")


def test_pane_picks_up_icons_from_appearance_settings(tmp_path):
    """`SupervisorPane.__init__` builds its glyph table once from `[appearance] icons` -- with it
    on, THE PIT's state column draws from `NERD` instead of the plain unicode table."""
    cfg = dataclasses.replace(make_config(tmp_path), appearance={"icons": True})
    app = SupervisorApp(cfg=cfg, window_source=lambda: [])
    assert app.pane.glyphs == glyphs_mod.icon_table("unicode", True)
    assert app.pane.glyphs["working"] == glyphs_mod.NERD["working"]


def test_extra_columns_appear_at_desk_width(tmp_path):
    """D tier: five columns, wider than the phone/narrow layouts, but
    `session`/`folder` have left the table for the detail panel -- see
    `test_detail_panel_shows_session_model_and_folder_at_desk_width` below."""
    drawn = run_at(tmp_path, 100)
    assert len(drawn["columns"]) == len(WIDE_COLUMNS)
    assert drawn["columns"] == ["state", "project", "where", "last action", "age"]
    state_cell = drawn["cells"][0]
    assert state_cell.startswith("!!")
    assert "blocked - permission" in state_cell
    assert "!! blocked - permission" in drawn["screen"]
    assert drawn["table_width"] <= drawn["viewport_width"]


def test_where_names_the_tmux_window_when_there_is_one(tmp_path):
    drawn = run_at(tmp_path, 80, windows=[PANTHEON_WINDOW])
    assert drawn["cells"][2] == "pantheon:3"
    assert "pantheon:3" in drawn["screen"]
    assert drawn["rows"][0].where == "pantheon:3"


def test_where_says_desktop_when_the_agent_is_in_no_tmux(tmp_path):
    drawn = run_at(tmp_path, 80, windows=[])
    # `AgentState.where` still says `desktop` (the detail panel and footer sentences use it) --
    # only the table's `where` COLUMN says `no window`, since that is what explains the missing
    # Jump/Kill buttons.
    assert drawn["cells"][2] == "no window"
    assert drawn["rows"][0].where == "desktop"


def test_jump_on_a_desktop_row_says_so_in_amber(tmp_path):
    """The quiet version of this was missed on the phone: nothing moved and nothing explained."""
    from pantheon.theme import TOKENS

    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [])
    seen = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            status = app.query_one("#status", Static)
            seen["status"] = str(status.content)
            seen["styles"] = styles_of(status)
            seen["screen"] = screen_text(app.export_screenshot())

    asyncio.run(drive())
    assert NOT_IN_TMUX_JUMP in seen["status"]
    assert "it lives on the desktop" in seen["screen"]
    assert TOKENS["warning"].lower() in seen["styles"].lower()   # amber Caution, never red
    assert TOKENS["error"].lower() not in seen["styles"].lower()


def test_pane_keys_fire_while_the_table_has_focus(tmp_path):
    """Bindings live on the pane; Textual walks the focus chain out from the DataTable."""
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [])
    seen = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            seen["focused"] = app.focused.id if app.focused else None
            await pilot.press("k")
            await pilot.pause()
            seen["after_k"] = str(app.query_one("#status", Static).content)
            await pilot.press("r")
            await pilot.pause()
            seen["after_r"] = str(app.query_one("#status", Static).content)

    asyncio.run(drive())
    assert seen["focused"] == "agents"
    assert NOT_IN_TMUX_KILL in seen["after_k"]
    assert NOT_IN_TMUX_KILL not in seen["after_r"]      # `r` cleared the sentence and reloaded


def test_question_mark_cycles_keys_then_connectors_then_closes(tmp_path):
    """the `?` overlay gained a second page (the connector gap list), so it is now a
    three-press cycle -- keys, connectors, closed -- not a plain toggle."""
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [])
    seen = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            panel = app.query_one("#keys", Static)
            seen["closed_at_start"] = panel.display
            await pilot.press("question_mark")
            await pilot.pause()
            seen["open"] = panel.display
            seen["page1_text"] = str(panel.content)
            await pilot.press("question_mark")
            await pilot.pause()
            seen["still_open"] = panel.display
            seen["page2_text"] = str(panel.content)
            await pilot.press("question_mark")
            await pilot.pause()
            seen["closed_again"] = panel.display

    asyncio.run(drive())
    assert seen["closed_at_start"] is False
    assert seen["open"] is True
    assert seen["still_open"] is True
    assert seen["closed_again"] is False
    assert "jump to that agent's window" in seen["page1_text"]
    assert "kill that window, asks first" in seen["page1_text"]
    assert "MCP servers the CLI sees" in seen["page2_text"]
    assert "claude.ai connectors" in seen["page2_text"]


def test_kill_opens_a_confirm_modal_and_y_closes_the_window(tmp_path, monkeypatch):
    """the inline y/n footer question is replaced by the one modal
    shape every confirm/pick now uses; `y` still closes it, exactly as the user already knew."""
    from pantheon import tmuxctl
    from pantheon.widgets.modal import Confirm
    from textual.widgets import Button

    monkeypatch.setattr(tmuxctl, "kill_window", lambda *a, **k: True)
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])
    seen = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("k")
            await pilot.pause()
            seen["screens_open"] = len(app.screen_stack)
            seen["is_confirm"] = isinstance(app.screen, Confirm)
            seen["question"] = str(app.screen.query_one("#question", Static).content)
            # kill is destructive (copy review "interaction issue 1"): Yes is the red error variant.
            seen["yes_variant"] = app.screen.query_one("#yes", Button).variant
            await pilot.press("y")
            await pilot.pause()
            seen["screens_closed"] = len(app.screen_stack)
            seen["status"] = str(app.query_one("#status", Static).content)

    asyncio.run(drive())
    assert seen["screens_open"] == 2                     # the Confirm modal was pushed
    assert seen["is_confirm"] is True
    assert "close window 3 (hiking_log_v2)?" in seen["question"]
    assert seen["yes_variant"] == "error"
    assert seen["screens_closed"] == 1                    # and popped again after the answer
    assert "closed window 3" in seen["status"]


def test_kill_n_leaves_the_window_running(tmp_path):
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])
    seen = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("k")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            seen["status"] = str(app.query_one("#status", Static).content)
            seen["screens"] = len(app.screen_stack)

    asyncio.run(drive())
    assert seen["status"].startswith("left it running")
    assert seen["screens"] == 1


def test_amber_not_red_for_a_row_that_needs_the_user(tmp_path):
    from pantheon.supervisor.app import row_style
    from pantheon.theme import TOKENS

    drawn = run_at(tmp_path, 80)
    assert row_style(drawn["rows"][0]) == "warning"
    assert TOKENS["warning"] == "#E69F00"                   # Caution amber, never the error red


def test_a_row_dispatched_from_the_deck_renders(tmp_path):
    """Queue-dispatched agents carry a tracker id; the row still draws, and says where it came
    from until the agent's first real tool event replaces the text."""
    fixture = [
        {"ts": utcnow_iso(), "source": "pantheon", "event": "dispatch",
         "session_id": "job77776666", "cwd": CWD, "tracker_id": "sprint-row-12"},
    ]
    drawn = run_at(tmp_path, 120, fixture=fixture)
    assert drawn["rows"][0].tracker_id == "sprint-row-12"
    assert drawn["cells"][3] == "started from the deck"
    assert drawn["cells"][2] == "no window"
    assert "started from the deck" in drawn["screen"]


# ---------------------------------------------------------------- row toolbar and controls


class FakeTmuxRun:
    """Records every tmux call `session_ctl`/the pane makes; answers `pane_command` lookups so a
    row can look like a live Claude Code window without a real tmux server."""

    def __init__(self, pane_command: str = "claude") -> None:
        self.pane_command = pane_command
        self.calls = []

    def __call__(self, *args, tmux=None, check=False):
        self.calls.append(args)

        class _R:
            pass

        r = _R()
        r.returncode = 0
        r.stdout = self.pane_command if args and args[0] == "display-message" else ""
        return r

    def sends(self):
        return [c for c in self.calls if c and c[0] == "send-keys"]


def test_toolbar_shows_the_full_button_row_for_a_blocked_agent_at_desk_width(tmp_path):
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from pantheon.widgets.toolbar import RowToolbar
            from textual.widgets import Button

            bar = app.query_one(RowToolbar)
            return bar.display, [str(b.label) for b in bar.query(Button)]

    display, labels = asyncio.run(drive())
    assert display is True
    #: a blocked row answers its prompt from here; model/effort/mode left
    # this row because they type into the window, where a permission dialog is up.
    for expected in ("Allow once (a)", "Deny (d)", "View (v)", "Answer (j)", "Kill (k)",
                     "Folder (o)", "Copy id (c)"):
        assert expected in labels
    assert not any(label.startswith(("Model", "Effort", "Mode")) for label in labels)


def test_toolbar_collapses_to_one_button_on_the_phone(tmp_path):
    """P width only: the desk-first review (findings 3, 14) found the button
    row collapsing at 120 columns just because a panel happened to be under that -- the bar now
    decides purely from its own width, and 65 columns (the phone) is still below the toolbar's
    own collapse threshold."""
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(65, 26)) as pilot:
            await pilot.pause()
            from pantheon.widgets.toolbar import RowToolbar
            from textual.widgets import Button

            bar = app.query_one(RowToolbar)
            return [str(b.label) for b in bar.query(Button)]

    labels = asyncio.run(drive())
    assert labels == ["Actions… (Enter)"]


def test_toolbar_shows_real_buttons_not_collapsed_at_80_columns(tmp_path):
    """The N tier (80-87) used to collapse to `Actions… (Enter)` just because the panel was under
    the deck's old `WIDE_AT = 120` -- the whole point of finding 3. It must not any more."""
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            from pantheon.widgets.toolbar import RowToolbar
            from textual.widgets import Button

            bar = app.query_one(RowToolbar)
            return [str(b.label) for b in bar.query(Button)]

    labels = asyncio.run(drive())
    assert labels != ["Actions… (Enter)"]
    assert any(label.startswith("Kill") for label in labels)


def test_toolbar_drops_jump_and_kill_for_a_desktop_row(tmp_path):
    """A `desktop` row (Claude Desktop, or a plain terminal) has no tmux window to jump to or
    close -- `Answer`/`Kill` used to show up disabled, which still looked pressable. They are dropped entirely, the same call `Resume now` got."""
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from pantheon.widgets.toolbar import RowToolbar
            from textual.widgets import Button

            bar = app.query_one(RowToolbar)
            return [str(b.label) for b in bar.query(Button)]

    labels = asyncio.run(drive())
    assert not any(label.startswith("Answer") or label.startswith("Kill") for label in labels)
    assert any(label.startswith("Folder") for label in labels)  # the rest of the row still works
    #: nothing to type into, so allow/deny show disabled, not hidden.
    assert any(label.startswith("Allow") for label in labels)


def test_m_picks_a_model_and_types_the_slash_command(tmp_path, monkeypatch):
    from pantheon import tmuxctl

    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("o")           # Opus family
            await pilot.pause()
            await pilot.press("1")           # its newest version, 5.5 (no usage in tests)
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(drive())
    assert "asked window 3 to switch to claude-opus-5-5" in status
    sent = [c for c in fake.calls if c[:2] == ("send-keys", "-t")]
    assert ("send-keys", "-t", "pantheon:3", "-l", "/model claude-opus-5-5") in sent
    assert ("send-keys", "-t", "pantheon:3", "Enter") in sent


def test_jumping_hints_the_back_key_on_the_window_just_landed_on(tmp_path, monkeypatch):
    """`j` used to leave the user stranded with no signpost back except tmux's plain
    status line. Landing on an agent window now fires one
    `display-message` there naming the configured back key."""
    from pantheon import tmuxctl

    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()

    asyncio.run(drive())
    hints = [c for c in fake.calls
             if c and c[0] == "display-message" and "-p" not in c and "-t" in c]
    assert any(c[-1] == "press F12 to go back to the deck" for c in hints)


def test_the_back_key_hint_uses_the_configured_key_not_a_hardcoded_f12(tmp_path, monkeypatch):
    from pantheon import tmuxctl

    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    cfg = dataclasses.replace(make_config(tmp_path), keys={"back_to_deck": "F9"})
    app = SupervisorApp(cfg=cfg, window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()

    asyncio.run(drive())
    hints = [c for c in fake.calls
             if c and c[0] == "display-message" and "-p" not in c and "-t" in c]
    assert any(c[-1] == "press F9 to go back to the deck" for c in hints)


def test_e_picks_an_effort_level(tmp_path, monkeypatch):
    from pantheon import tmuxctl

    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            await pilot.press("3")           # EFFORT_LEVELS[2] == "high"
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(drive())
    assert "asked window 3 to switch to high" in status
    assert ("send-keys", "-t", "pantheon:3", "-l", "/effort high") in fake.calls


def test_p_presses_shift_tab_the_right_number_of_times(tmp_path, monkeypatch):
    from pantheon import tmuxctl

    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    fixture = FIXTURE + [
        {"ts": utcnow_iso(), "source": "claude", "event": "PostToolUse", "session_id": "abc12345deadbeef",
         "cwd": CWD, "tmux_pane": "%3", "tool_name": "Read", "permission_mode": "auto"},
    ]
    app = SupervisorApp(cfg=make_config(tmp_path, fixture), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("p")           # ("p", "plan") -- 3rd row of MODE_OPTIONS is "acceptEdits"; pick "plan"
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(drive())
    assert "asked window 3 to switch to plan mode" in status
    btabs = [c for c in fake.calls if c[-1] == "BTab"]
    assert len(btabs) == 3     # auto -> default -> acceptEdits -> plan


def test_model_is_refused_on_a_row_with_no_live_tmux_window(tmp_path, monkeypatch):
    from pantheon import tmuxctl
    from pantheon.supervisor.pane import NOT_TYPEABLE

    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            return str(app.query_one("#status", Static).content), len(app.screen_stack)

    status, screens = asyncio.run(drive())
    assert NOT_TYPEABLE in status
    assert screens == 1                       # no picker was opened
    assert fake.calls == []                   # nothing was typed


def test_w_width_adds_model_and_mode_columns(tmp_path):
    drawn = run_at(tmp_path, 180, windows=[PANTHEON_WINDOW])
    assert len(drawn["columns"]) == len(SUPER_WIDE_COLUMNS)
    assert drawn["columns"][-2:] == ["model", "mode"]
    assert drawn["cells"][-2] == "-"          # no statusline capture in this test
    assert drawn["cells"][-1] == "-"          # no permission_mode reported yet


# ---------------------------------------------------------------- B.4: the detail panel


def test_detail_panel_hidden_below_desk_width(tmp_path):
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            return app.query_one("#detail", Static).display

    assert asyncio.run(drive()) is False


def test_detail_panel_shows_session_model_and_folder_at_desk_width(tmp_path):
    """`session`/`folder` left the agents table at D/W; this is where they went
   ."""
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])
    seen = {}

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            detail = app.query_one("#detail", Static)
            seen["display"] = detail.display
            seen["title"] = detail.border_title
            seen["body"] = str(detail.content)

    asyncio.run(drive())
    assert seen["display"] is True
    assert seen["title"] == "hiking_log_v2 · window 3"
    body = seen["body"]
    assert "blocked - permission" in body
    assert "session " in body and "abc12345" in body      # no statusline capture: short id
    assert "model -" in body and "effort -" in body and "mode -" in body
    assert "where pantheon:3" in body
    assert "folder " in body and "hiking_log_v2" in body
    assert "last five actions" in body
    assert "Claude needs your permission to use Bash" in body   # the fixture's Notification message


def test_detail_panel_changes_when_a_different_row_is_selected(tmp_path):
    fixture = [
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "abc12345deadbeef", "cwd": CWD, "tmux_pane": "%3"},
        {"ts": utcnow_iso(), "source": "claude", "event": "Notification",
         "session_id": "abc12345deadbeef", "cwd": CWD, "tmux_pane": "%3",
         "notification_type": "permission_prompt",
         "message": "Claude needs your permission to use Bash"},
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "zzz999otherrow00", "cwd": "C:/Home/x/Documents/Projects/Plumb",
         "tmux_pane": "%9"},
        {"ts": utcnow_iso(), "source": "claude", "event": "PostToolUse",
         "session_id": "zzz999otherrow00", "cwd": "C:/Home/x/Documents/Projects/Plumb",
         "tmux_pane": "%9", "tool_name": "Edit"},
    ]
    app = SupervisorApp(cfg=make_config(tmp_path, fixture), window_source=lambda: [])
    seen = {}

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            detail = app.query_one("#detail", Static)
            seen["first"] = detail.border_title
            table = app.query_one("#agents", DataTable)
            table.move_cursor(row=1)
            await pilot.pause()
            seen["second"] = detail.border_title

    asyncio.run(drive())
    assert seen["first"] != seen["second"]
    assert {seen["first"], seen["second"]} == {"hiking_log_v2 · desktop", "Plumb · desktop"}


def test_subtitle_names_the_row_count_and_sort(tmp_path):
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            return app.pane.subtitle()

    subtitle = asyncio.run(drive())
    assert subtitle == "1 running · sorted by who needs you"


# ---------------------------------------------------------------- B.6: the standalone frame


def test_standalone_app_is_framed_at_desk_width(tmp_path):
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [])
    seen = {}

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            from textual.containers import Vertical

            frame = app.query_one("#frame", Vertical)
            seen["classes"] = frame.classes
            seen["title"] = frame.border_title
            seen["subtitle"] = frame.border_subtitle

    asyncio.run(drive())
    assert "framed" in seen["classes"]
    assert seen["title"] == "THE PIT · agents"
    assert "running" in (seen["subtitle"] or "")


def test_standalone_app_has_no_frame_on_the_phone(tmp_path):
    """Below 80 columns: no border (the phone) -- and the N tier (80-87) must not have its usable
    width eaten by a border that would push it back into the phone layout (the design floor: "the
    80-column N tier stays as it is")."""
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [])
    seen = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            from textual.containers import Vertical
            from textual.widgets import DataTable

            frame = app.query_one("#frame", Vertical)
            seen["classes"] = frame.classes
            seen["columns"] = [str(c.label) for c in app.query_one("#agents", DataTable).columns.values()]

    asyncio.run(drive())
    assert "framed" not in seen["classes"]
    assert seen["columns"] == ["state", "project", "where", "last action", "age"]   # N tier intact


def test_hand_off_confirms_with_the_digest_then_dispatches(tmp_path, monkeypatch):
    """`governor.handoff` module now exists; the toolbar's `h` wires to it (S-UI 6.2): fetch
    the digest in a worker, confirm with its first lines shown, then dispatch on `y`."""
    from pantheon.supervisor import pane as pane_mod

    fixture = [
        {"ts": utcnow_iso(), "source": "codex", "event": "queued", "job_id": "job1", "project": "Plumb"},
        {"ts": utcnow_iso(), "source": "codex", "event": "done", "job_id": "job1", "project": "Plumb",
         "detail": "exit 2"},
    ]
    seen_args = {}

    def fake_digest_for(row, cfg, **kw):
        return "line one\nline two", "the last 2 lines of job1.log"

    def fake_handoff(row, to_provider, cfg, providers, agent_rows, **kw):
        seen_args["to_provider"] = to_provider
        seen_args["digest_fn_result"] = kw["digest_fn"](row, cfg)
        from pantheon.models import LaunchResult

        return LaunchResult(ok=True, message="claude is working in window 5")

    monkeypatch.setattr(pane_mod.handoff_mod, "digest_for", fake_digest_for)
    monkeypatch.setattr(pane_mod.handoff_mod, "handoff", fake_handoff)
    app = SupervisorApp(cfg=make_config(tmp_path, fixture), window_source=lambda: [])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            await app.workers.wait_for_complete()
            await pilot.pause()
            from pantheon.widgets.modal import Confirm
            from textual.widgets import Button

            is_confirm = isinstance(app.screen, Confirm)
            question = str(app.screen.query_one("#question", Static).content) if is_confirm else ""
            body = str(app.screen.query_one("#body", Static).content) if is_confirm else ""
            # hand-off is destructive too (it retires the dead session): Yes is the red error variant.
            yes_variant = app.screen.query_one("#yes", Button).variant if is_confirm else ""
            await pilot.press("y")
            await pilot.pause()
            return is_confirm, question, body, yes_variant, str(app.query_one("#status", Static).content)

    is_confirm, question, body, yes_variant, status = asyncio.run(drive())
    assert is_confirm is True
    assert "Plumb" in question and "Claude" in question
    assert "It gets the task briefing plus what the last agent did." in question
    assert yes_variant == "error"
    assert "line one" in body and "line two" in body
    assert seen_args["to_provider"] == "claude"
    assert seen_args["digest_fn_result"] == ("line one\nline two", "the last 2 lines of job1.log")
    assert "claude is working in window 5" in status


def test_hand_off_is_refused_when_the_target_provider_is_off(tmp_path, monkeypatch):
    from pantheon.supervisor import pane as pane_mod

    fixture = [
        {"ts": utcnow_iso(), "source": "codex", "event": "queued", "job_id": "job2", "project": "Plumb"},
        {"ts": utcnow_iso(), "source": "codex", "event": "done", "job_id": "job2", "project": "Plumb",
         "detail": "exit 1"},
    ]
    cfg = dataclasses.replace(make_config(tmp_path, fixture),
                              providers={"claude": False, "codex": True, "ollama": False})
    called = []
    monkeypatch.setattr(pane_mod.handoff_mod, "digest_for", lambda *a, **k: called.append(1))
    app = SupervisorApp(cfg=cfg, window_source=lambda: [])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            return str(app.query_one("#status", Static).content), len(app.screen_stack)

    status, screens = asyncio.run(drive())
    assert "'claude' is not switched on" in status
    assert screens == 1
    assert called == []


def test_enter_opens_the_actions_picker_at_narrow_width(tmp_path):
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(65, 26)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            from pantheon.widgets.modal import Pick

            is_pick = isinstance(app.screen, Pick)
            await pilot.press("escape")
            await pilot.pause()
            return is_pick, len(app.screen_stack)

    is_pick, screens = asyncio.run(drive())
    assert is_pick is True
    assert screens == 1
