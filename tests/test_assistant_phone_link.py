"""The Assistant's phone link, name and model.

- The deck reads `Remote Control disconnected` / `/rc failed` off the Assistant's screen through
  the startup-question scan it already runs (no extra tmux call), and the sidebar line says
  `phone link off` in the warning colour with one NEEDS YOU line.
- `L` (or the `↻ reconnect` line) types `/remote-control` only after a fresh read shows the
  Assistant idle at an empty prompt, and reads Claude's own menu line before pressing Enter
  (the command toggles).
- A NEW Assistant conversation is started with `--name`; a resumed one never is. Model and
  effort go on the command line every time.
No real tmux and no real `claude`."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pantheon import assistant as assistant_mod
from pantheon import config as config_mod
from pantheon import dialogs as dialogs_mod
from pantheon import tmuxctl
from pantheon.dispatch import sessions as sessions_mod
from pantheon.models import AgentState, AgentStatus
from pantheon.providers.claude import claude_command

ROOT = "C:/Home/x/Documents/Projects"
RULE = "─" * 70
NAMED_RULE = "─" * 50 + " Probe Two ─"
STATUS = "  5h 64% · wk 58% · ctx 6% · $0.48 · Opus 5.5"


def screen(above: list[str], typed: str = "", status: str = STATUS, rule_top: str = RULE) -> str:
    return "\n".join(above + ["", rule_top, f"> {typed}".rstrip() + (" " if not typed else ""),
                              RULE, status, "  ⏸ manual mode on", "", ""])


NOTICE = ("  ⎿  Remote Control disconnected — this session was ended or archived from another "
          "device or app (code 4090)")
ACTIVE = "  ⎿  /remote-control is active · Continue here, on your phone, or at https://claude.ai/code/x"
LOST = screen(["● Done.", NOTICE])
FINE = screen(["● Done.", ACTIVE])
MENU_CONNECT = screen(["", "  /remote-control     Control this session from your phone, tablet or browser"],
                      typed="/remote-control")
MENU_DISCONNECT = screen(["", "  /remote-control                    Disconnect Remote Control"],
                         typed="/remote-control", rule_top=NAMED_RULE)
BACK = screen(["● Done.", NOTICE, ACTIVE])


# --------------------------------------------------------------------------- reading the screen


def test_the_disconnect_notice_or_rc_failed_means_the_link_is_lost():
    assert dialogs_mod.link_lost(LOST)
    assert dialogs_mod.link_lost(screen(["● Done."], status=STATUS + " · /rc failed"))
    assert not dialogs_mod.link_lost(FINE)
    assert not dialogs_mod.link_lost(BACK)                 # a reconnect after the notice wins
    assert not dialogs_mod.link_lost("")


def test_talk_about_the_phone_link_is_not_the_notice():
    chat = screen(["● It said Remote Control disconnected earlier, so I looked into it."])
    assert not dialogs_mod.link_lost(chat)


def test_idle_prompt_is_the_only_state_worth_typing_into():
    assert dialogs_mod.idle_prompt(LOST)
    assert dialogs_mod.idle_prompt(screen(["x"], typed='Try "fix the tests"'))
    assert not dialogs_mod.idle_prompt(screen(["x"], typed="half a sentence"))
    assert not dialogs_mod.idle_prompt(screen(["✻ Thinking… (esc to interrupt)"]))
    assert not dialogs_mod.idle_prompt("plain shell $")


def test_the_scan_remembers_the_link_per_pane_without_an_extra_read():
    reads = []

    def capture(target, tmux=None):
        reads.append(target)
        return LOST

    scanner = dialogs_mod.Scanner(capture=capture)
    row = AgentState(session_id="s", provider="claude", status=AgentStatus.WAITING_INPUT,
                     window_index=3, tmux_session="pantheon", tmux_pane="%3")
    scanner.scan([row])
    assert reads == ["pantheon:3"]
    assert scanner.link_lost("%3") and not scanner.link_lost("%9")
    scanner.scan([])                                      # busy now: not a candidate, not re-read
    assert scanner.link_lost("%3") and reads == ["pantheon:3"]


# --------------------------------------------------------------------------- the sidebar line


def make_cfg(tmp_path, **assistant) -> config_mod.Config:
    cfg = config_mod.Config(state_dir=str(tmp_path / "state"), projects_root=ROOT,
                            claude_home=str(tmp_path / "claude_home"), assistant=assistant)
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    return cfg


def save_state(cfg, **record) -> None:
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    (Path(cfg.state_dir) / "assistant.json").write_text(json.dumps(record), encoding="utf-8")


def the_row(status=AgentStatus.WAITING_INPUT):
    return AgentState(session_id="5f40ea10", provider="claude", status=status, project="workspace",
                      cwd=ROOT, window_index=3, tmux_session="pantheon", tmux_pane="%3")


def test_a_lost_link_turns_the_line_amber_and_names_the_conversation(tmp_path):
    cfg = make_cfg(tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "custom-title", "customTitle": "Pocket Nick Fury",
                                      "sessionId": "5f40ea10"}) + "\n", encoding="utf-8")
    save_state(cfg, session_id="5f40ea10", pane_id="%3", transcript_path=str(transcript))
    view = assistant_mod.view(cfg, [the_row()], [], link_lost=lambda key: key == "%3")
    assert view.status == assistant_mod.PHONE_OFF and view.style == "warning" and view.link_lost
    assert view.name == "Pocket Nick Fury"
    fine = assistant_mod.view(cfg, [the_row(AgentStatus.IDLE)], [], link_lost=lambda key: False)
    assert fine.status == assistant_mod.IDLE and not fine.link_lost
    # A question on screen outranks the phone link: that is what the user must answer first.
    asking = assistant_mod.view(cfg, [the_row(AgentStatus.BLOCKED_PERMISSION)], [],
                                link_lost=lambda key: True)
    assert asking.status == assistant_mod.NEEDS_YOU


def test_no_title_record_means_no_name(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "ai-title", "aiTitle": "Checking the deck"}) + "\n",
                          encoding="utf-8")
    assert sessions_mod.custom_title(transcript) is None
    assert sessions_mod.custom_title(tmp_path / "missing.jsonl") is None


# --------------------------------------------------------------------------- reconnect


class Keys:
    """A fake tmux that answers `list-panes` with the Assistant's pane and records every key."""

    def __init__(self):
        self.sent: list[tuple] = []

    def __call__(self, *args, tmux=None, check=False):
        if args[0] == "list-panes":
            return SimpleNamespace(returncode=0, stdout="%3|3|ASSISTANT|node", stderr="")
        if args[0] == "send-keys":
            self.sent.append(args[3:])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")


def reconnect_with(tmp_path, monkeypatch, screens: list[str]):
    cfg = make_cfg(tmp_path)
    save_state(cfg, session_id="5f40ea10", pane_id="%3")
    keys = Keys()
    monkeypatch.setattr(tmuxctl, "run", keys)
    queue = list(screens)

    def capture(target, tmux=None):
        assert target == "%3"
        return queue.pop(0) if len(queue) > 1 else queue[0]

    result = assistant_mod.reconnect(cfg, tmux="tmux", capture=capture, sleep=lambda s: None)
    return result, keys.sent


def test_a_fine_link_sends_nothing(tmp_path, monkeypatch):
    result, sent = reconnect_with(tmp_path, monkeypatch, [FINE])
    assert result["message"] == assistant_mod.MSG_RC_FINE and sent == []


def test_a_busy_assistant_is_never_typed_into(tmp_path, monkeypatch):
    busy = screen(["✻ Working… (esc to interrupt)", NOTICE])
    result, sent = reconnect_with(tmp_path, monkeypatch, [busy])
    assert not result["ok"] and result["message"] == assistant_mod.MSG_RC_BUSY and sent == []
    typed = screen([NOTICE], typed="a half-typed thought")
    result, sent = reconnect_with(tmp_path, monkeypatch, [typed])
    assert result["message"] == assistant_mod.MSG_RC_BUSY and sent == []


def test_connect_types_the_command_reads_the_menu_then_presses_enter(tmp_path, monkeypatch):
    result, sent = reconnect_with(tmp_path, monkeypatch, [LOST, MENU_CONNECT, BACK])
    assert result["ok"] and result["message"] == assistant_mod.MSG_RC_BACK
    assert sent == [("-l", "/remote-control"), ("Enter",)]


def test_a_dead_link_it_still_holds_is_dropped_then_made_again(tmp_path, monkeypatch):
    result, sent = reconnect_with(tmp_path, monkeypatch,
                                  [LOST, MENU_DISCONNECT, LOST, MENU_CONNECT, BACK])
    assert result["ok"]
    assert sent == [("-l", "/remote-control"), ("Enter",), ("-l", "/remote-control"), ("Enter",)]


def test_an_unexpected_menu_clears_what_was_typed_and_stops(tmp_path, monkeypatch):
    result, sent = reconnect_with(tmp_path, monkeypatch, [LOST, screen([NOTICE], typed="/remote-control")])
    assert not result["ok"] and result["message"] == assistant_mod.MSG_RC_NO_MENU
    assert sent[0] == ("-l", "/remote-control") and ("Enter",) not in sent
    assert sent[1] == ("Escape",) and sent[2] == ("-N", "15", "BSpace")


# --------------------------------------------------------------------------- name, model, effort


def test_the_command_line_carries_model_effort_and_a_name_only_when_asked():
    fresh = claude_command("claude", {"start_model": "claude-opus-5-5", "start_effort": "medium",
                                      "name": "Pantheon Assistant"})
    assert fresh.endswith(' --model claude-opus-5-5 --effort medium --name "Pantheon Assistant"')
    resumed = claude_command("claude", {"resume": "5f40ea10", "start_model": "claude-opus-5-5",
                                        "start_effort": "medium"})
    assert "--resume 5f40ea10 --model claude-opus-5-5 --effort medium" in resumed
    assert "--name" not in resumed
    odd = claude_command("claude", {"start_model": "opus; rm -rf", "start_effort": "ludicrous",
                                    "start_mode": "yolo"})
    assert "--model" not in odd and "--effort" not in odd and "--permission-mode" not in odd
    assert claude_command("claude", {"start_mode": "plan"}) == "claude --permission-mode plan"


def test_the_assistant_settings_default_to_opus_medium_and_can_be_blanked():
    cfg = config_mod.Config()
    settings = cfg.assistant_settings()
    assert (settings.model, settings.effort, settings.name) == ("claude-opus-5-5", "medium",
                                                                "Pantheon Assistant")
    blank = config_mod.Config(assistant={"model": "", "effort": " ", "name": ""}).assistant_settings()
    assert (blank.model, blank.effort, blank.name) == ("", "", "")


def test_the_sidebar_shows_phone_link_off_and_a_reconnect_line(tmp_path):
    import asyncio

    from textual.app import App, ComposeResult

    from pantheon.supervisor.rail import SessionSidebar

    cfg = make_cfg(tmp_path)
    got = {}
    lost = assistant_mod.View(assistant_mod.PHONE_OFF, "warning", "5f40ea10", "%3", 3, frozenset(),
                              "Pocket Nick Fury", True)

    class _App(App):
        def compose(self) -> ComposeResult:
            yield SessionSidebar(cfg, rows_source=lambda: [], entries_source=lambda r: [],
                                 assistant_source=lambda: lost, id="rail")

    async def drive():
        app = _App()
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.pause()
            rail = app.query_one("#rail")
            plan = rail._plan()
            at = [s[0] for s in plan].index("assistant")
            got["kinds"] = [s[0] for s in plan][at:at + 2]
            got["line"] = rail._row_text(plan[at]).plain
            got["reconnect"] = rail._row_text(plan[at + 1]).plain

    asyncio.run(drive())
    assert got["kinds"] == ["assistant", "reconnect"]
    assert got["line"].splitlines()[0].endswith("phone link off")
    assert got["line"].splitlines()[1].strip() == "Pocket Nick Fury"
    assert "(L)" in got["reconnect"]
