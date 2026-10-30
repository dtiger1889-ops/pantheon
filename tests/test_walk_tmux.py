"""Scripted walk of the deck on a throwaway tmux server.

Runs only on request, inside the tmux: `pytest -m walk` (plain `pytest` excludes the `walk` marker
through `addopts`). Stand-in agents only (`tests/fixtures/fake_agent.sh` run as a copy of bash named
`claude.exe`) -- never a real Claude or Codex. The deck runs on the throwaway server of
`tests/tmux_harness.py` (private TMUX_TMPDIR, PANTHEON_SESSION=pantheon-test, empty $HOME); nothing
here names the live `pantheon` session. A failing wait carries the pane capture in its message.

Where the live deck differs from its design notes the test asserts what the deck does today and
says so: `stage.JOIN_PANES` is False, so Enter on a running session jumps to its
own window instead of opening it beside the list.
"""
from __future__ import annotations

import time

import pytest

from tests.tmux_harness import SESSION, _tmux_reachable, launcher, walk_deck, wait_until  # noqa: F401

pytestmark = [
    pytest.mark.real_tmux,
    pytest.mark.walk,
    pytest.mark.skipif(not _tmux_reachable(), reason="no reachable tmux (Desktop-app session)"),
]

DECK = f"{SESSION}:0"


def _boot(lab, tmp_path, mode="idle", events=None, name="alpha"):
    """A fake session under a project folder, a client attached, the deck drawn with its row."""
    agent = lab.fake_agent(name, tmp_path / "proj" / name,
                           events or [{"event": "UserPromptSubmit"}, {"event": "Stop"}], mode=mode)
    lab.attach_client()
    lab.wait_for_text(DECK, "PANTHEON", timeout=60)
    lab.wait_for_text(DECK, "THE PIT", timeout=60)
    lab.wait_for_text(DECK, f"window {agent['index']}", timeout=60)
    return agent


def _permission_events():
    return [{"event": "UserPromptSubmit"},
            {"event": "Notification", "notification_type": "permission_prompt",
             "message": "Claude needs your permission to use Bash"}]


def test_enter_on_a_running_session_goes_to_its_window_and_f12_comes_back(walk_deck, tmp_path):
    """Click-to-stage: JOIN_PANES is off, so 'opens beside the list' is the jump."""
    lab = walk_deck
    agent = _boot(lab, tmp_path)
    lab.wait_for_text(DECK, "1 running")
    time.sleep(3.0)                                  # first test of a run: let the deck settle
    lab.press(DECK, "Tab")
    time.sleep(1.0)
    lab.press(DECK, "Down", "Down", "Down", delay=0.6)
    before_enter = lab.screen(DECK)
    lab.press(DECK, "Enter")
    try:
        lab.wait_for_client_window(agent["index"])
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n--- deck before Enter ---\n{before_enter}\n--- deck after ---\n"
                             f"{lab.screen(DECK)}") from None
    assert "fake-agent ready" in lab.wait_for_text(f"{SESSION}:{agent['index']}", "fake-agent ready")
    assert len(lab.tmux("list-panes", "-t", f"{SESSION}:0", "-F", "#{pane_id}").stdout.split()) == 1, \
        "a pane was joined into the deck window although JOIN_PANES is off"
    lab.press_client("F12")
    lab.wait_for_client_window(0)


def test_b_with_nothing_staged_leaves_the_deck_alone(walk_deck, tmp_path):
    lab = walk_deck
    _boot(lab, tmp_path)
    before = lab.windows()
    lab.press(DECK, "b")
    time.sleep(1.0)
    assert lab.windows() == before, f"b changed the windows: {before} -> {lab.windows()}"
    assert "PANTHEON" in lab.screen(DECK)


def test_capital_A_with_the_assistant_off_says_so_and_changes_nothing(walk_deck, tmp_path):
    """The Assistant start types a real `claude` into a shell, so it is never started here; the
    throwaway deck runs with PANTHEON_ASSISTANT=off and `A` must not open a window."""
    lab = walk_deck
    _boot(lab, tmp_path)
    before = {n for _, n, _ in lab.windows()}
    lab.press(DECK, "A")
    time.sleep(2.0)
    assert {n for _, n, _ in lab.windows()} == before, "A opened a window with the Assistant off"
    assert "PANTHEON" in lab.screen(DECK)


def test_a_on_a_permission_prompt_shows_it_then_allows(walk_deck, tmp_path):
    lab = walk_deck
    agent = _boot(lab, tmp_path, mode="permission", events=_permission_events())
    lab.wait_for_text(DECK, "blocked - permission")
    lab.press(DECK, "a")
    lab.wait_for_text(DECK, "a allow once")          # the prompt is shown first (hook not registered)
    time.sleep(1.0)                                  # let the request screen take keys
    lab.press(DECK, "a")
    lab.wait_for_text(f"{SESSION}:{agent['index']}", "FAKE-KEY:1")


def test_d_on_a_permission_prompt_answers_no(walk_deck, tmp_path):
    lab = walk_deck
    agent = _boot(lab, tmp_path, mode="permission", events=_permission_events())
    lab.wait_for_text(DECK, "blocked - permission")
    lab.press(DECK, "d")
    lab.wait_for_text(DECK, "d deny")
    time.sleep(1.0)
    lab.press(DECK, "d")
    lab.wait_for_text(f"{SESSION}:{agent['index']}", "FAKE-KEY:4")


def test_v_shows_the_whole_request_and_esc_closes_it(walk_deck, tmp_path):
    lab = walk_deck
    agent = _boot(lab, tmp_path, mode="permission", events=_permission_events())
    lab.wait_for_text(DECK, "blocked - permission")
    lab.press(DECK, "v")
    lab.wait_for_text(DECK, "Do you want to proceed?")
    lab.press(DECK, "Escape")
    lab.wait_for_gone(DECK, "a allow once")
    assert "FAKE-KEY" not in lab.screen(f"{SESSION}:{agent['index']}"), "v typed into the agent"


def test_H_hand_off_asks_then_types_checkpoint_into_the_session(walk_deck, tmp_path):
    lab = walk_deck
    agent = _boot(lab, tmp_path)
    lab.press(DECK, "H")
    lab.wait_for_text(DECK, "It gets asked to /checkpoint")
    lab.press(DECK, "y")
    lab.wait_for_text(f"{SESSION}:{agent['index']}", "FAKE-LINE:/checkpoint", timeout=30)


def test_m_model_picker_opens_and_esc_closes_it(walk_deck, tmp_path):
    lab = walk_deck
    _boot(lab, tmp_path)
    lab.press(DECK, "m")
    lab.wait_for_text(DECK, "pick a model")
    lab.press(DECK, "Escape")
    lab.wait_for_gone(DECK, "pick a model")


def test_e_effort_picker_opens_and_esc_closes_it(walk_deck, tmp_path):
    lab = walk_deck
    _boot(lab, tmp_path)
    lab.press(DECK, "e")
    lab.wait_for_text(DECK, "pick an effort level")
    lab.press(DECK, "Escape")
    lab.wait_for_gone(DECK, "pick an effort level")


def test_capital_E_opens_the_file_editor_and_again_does_not_open_a_second(walk_deck, tmp_path):
    lab = walk_deck
    _boot(lab, tmp_path)
    target = tmp_path / "walk-note.txt"
    target.write_text("hello\n", encoding="utf-8")
    lab.press(DECK, "E")
    lab.wait_for_text(DECK, "edit which file?")
    lab.type_text(DECK, str(target).replace("\\", "/"))
    lab.press(DECK, "Enter")
    wait_until(lambda: any("EDIT" in n for _, n, _ in lab.windows()) or
               len(lab.tmux("list-panes", "-t", DECK, "-F", "#{pane_id}").stdout.split()) > 1,
               f"no editor appeared; windows {lab.windows()}\n{lab.screen(DECK)}", timeout=20)
    count = len(lab.windows()), len(lab.tmux("list-panes", "-t", DECK, "-F", "#{pane_id}").stdout.split())
    lab.press(DECK, "E")
    time.sleep(2.0)
    after = len(lab.windows()), len(lab.tmux("list-panes", "-t", DECK, "-F", "#{pane_id}").stdout.split())
    assert after == count, f"E opened another editor: {count} -> {after}\n{lab.windows()}"


def test_j_on_an_agent_row_jumps_to_its_window(walk_deck, tmp_path):
    lab = walk_deck
    agent = _boot(lab, tmp_path)
    lab.press(DECK, "j")
    lab.wait_for_client_window(agent["index"])


def test_P_opens_the_pit_and_F12_returns_to_the_deck(walk_deck, tmp_path):
    lab = walk_deck
    _boot(lab, tmp_path)
    lab.press(DECK, "P")
    wait_until(lambda: any(n == "PIT" for _, n, _ in lab.windows()),
               f"no PIT window; windows {lab.windows()}\n{lab.screen(DECK)}", timeout=20)
    pit = next(i for i, n, _ in lab.windows() if n == "PIT")
    lab.wait_for_client_window(pit)
    lab.press_client("F12")
    lab.wait_for_client_window(0)


def test_question_mark_pages_show_keys_connectors_and_the_tools_page(walk_deck, tmp_path):
    """The tools page is the third `?` page."""
    lab = walk_deck
    _boot(lab, tmp_path)
    lab.press(DECK, "?")
    lab.wait_for_text(DECK, "these keys work in every window")
    lab.press(DECK, "?")
    lab.wait_for_text(DECK, "MCP servers the CLI sees")
    lab.press(DECK, "?")
    lab.wait_for_text(DECK, "SKILLS")
    lab.press(DECK, "Escape")
    lab.wait_for_text(DECK, "THE PIT")


def test_F4_opens_the_prompt_scratchpad(walk_deck, tmp_path):
    """F4 from anywhere creates and selects the NOTES window."""
    lab = walk_deck
    _boot(lab, tmp_path)
    lab.press_client("F4")
    wait_until(lambda: any(n == "NOTES" for _, n, _ in lab.windows()),
               f"no NOTES window; windows {lab.windows()}", timeout=30)
    notes = next(i for i, n, _ in lab.windows() if n == "NOTES")
    lab.wait_for_client_window(notes)
    lab.wait_for_text(f"{SESSION}:{notes}", "notes", timeout=30)


def test_R_menu_lists_restart_everything_first_and_deck_only_second(walk_deck, tmp_path):
    lab = walk_deck
    _boot(lab, tmp_path)
    lab.press(DECK, "R")
    screen = lab.wait_for_text(DECK, "restart what?")
    assert "Restart everything" in screen and "Restart deck only" in screen, screen
    assert screen.index("Restart everything") < screen.index("Restart deck only"), screen
    lab.press(DECK, "Escape")
    lab.wait_for_gone(DECK, "restart what?")


def test_f2_and_f3_reach_the_queue_and_budget_windows(walk_deck, tmp_path):
    lab = walk_deck
    _boot(lab, tmp_path)
    lab.press_client("F2")
    lab.wait_for_client_window(1)
    lab.press_client("F3")
    lab.wait_for_client_window(2)
    lab.press_client("F1")
    lab.wait_for_client_window(0)
