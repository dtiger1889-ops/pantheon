""": answering permission prompts from the deck.

The dialog screens are real captures, and so is the hook record shape (the same run, the
worktree's `hooks/pantheon_event.ps1` registered through `claude --settings`). The deck's tmux
is faked: `tmuxctl.run` is replaced, so no key ever reaches a real window.

Numbered comments name the spec's acceptance checks each test covers.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pantheon import config as config_mod
from pantheon import tmuxctl
from pantheon.models import AgentStatus, TmuxWindow
from pantheon.session_view import approval as ap
from pantheon.supervisor import approvals as approvals_mod
from pantheon.supervisor.app import SupervisorApp

FIX = Path(__file__).parent / "fixtures" / "approvals"
BASH = (FIX / "bash_2.1.283.txt").read_text(encoding="utf-8")
BASH_LONG = (FIX / "bash_long_2.1.283.txt").read_text(encoding="utf-8")
WRITE = (FIX / "write_2.1.283.txt").read_text(encoding="utf-8")
ASK = (FIX / "ask_2.1.283.txt").read_text(encoding="utf-8")
TRUST = (Path(__file__).parent / "fixtures" / "dialogs" / "trust_folder_2.1.283.txt").read_text(encoding="utf-8")

CWD = "C:\\msys64\\tmp\\pw\\paprobe"
PROBE_CMD = "echo pantheon-probe-1 > probe1.txt"
LONG_CMD = ("cd /tmp/pw/paprobe && mkdir -p sub && echo alpha-bravo-charlie-delta-echo-foxtrot-golf-"
            "hotel-india-juliet-kilo-lima-mike-november > sub/long.txt && echo oscar-papa-quebec-"
            "romeo-sierra-tango-uniform-victor-whiskey-xray-yankee-zulu >> sub/long.txt")

# What Claude Code draws once a prompt is answered: the prompt box again, and either the spinner
# (it is running the command) or nothing (it stopped and asks what to do instead).
IDLE = ("● Done.\n" + "─" * 80 + "\n> \n" + "─" * 80 + "\n  5h 29% · wk 54% · Sonnet 5\n")
RUNNING = ("✻ Brewing… (esc to interrupt)\n" + "─" * 80 + "\n> \n" + "─" * 80 + "\n  5h 29%\n")
INTERRUPTED = ("  ⎿  Interrupted · What should Claude do instead?\n" + "─" * 80 + "\n> \n"
               + "─" * 80 + "\n  5h 29%\n")


def iso(seconds_ago: float = 0) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def request_event(sid, pane, tool, tool_input, cwd=CWD, ago=8):
    """The line `hooks/pantheon_event.ps1` wrote for a PermissionRequest in the live run."""
    return {"ts": iso(ago), "source": "claude", "event": "PermissionRequest", "session_id": sid,
            "cwd": cwd, "tool_name": tool, "notification_type": None, "message": None,
            "detail": None, "agent_id": None, "permission_mode": "default", "tmux_pane": pane,
            "tool_input": tool_input}


def notification_event(sid, pane, cwd=CWD, ago=2):
    return {"ts": iso(ago), "source": "claude", "event": "Notification", "session_id": sid,
            "cwd": cwd, "notification_type": "permission_prompt",
            "message": "Claude needs your permission", "tmux_pane": pane}


def start_event(sid, pane, cwd=CWD, ago=60):
    return {"ts": iso(ago), "source": "claude", "event": "SessionStart", "session_id": sid,
            "cwd": cwd, "tmux_pane": pane, "detail": "startup"}


class Panes:
    """A fake tmux: each pane shows a screen; digits and keys change it like Claude Code does."""

    def __init__(self, screens: dict[str, str], after: dict | None = None,
                 aliases: dict | None = None):
        self.screens = dict(screens)
        self.aliases = aliases or {}        # `pantheon:4` -> `%9`: one pane, two names
        self.after = after or {}            # (pane, key) -> the screen that key leads to
        self.keys: list[tuple[str, str]] = []

    def run(self, *args, tmux=None, check=False):
        out = ""
        if args and args[0] == "capture-pane":
            t = args[args.index("-t") + 1]
            out = self.screens.get(self.aliases.get(t, t), "")
        elif args and args[0] == "send-keys":
            target = args[args.index("-t") + 1]
            target = self.aliases.get(target, target)
            key = args[-1]
            self.keys.append((target, key))
            nxt = self.after.get((target, key))
            if nxt is not None:
                self.screens[target] = nxt
        return subprocess.CompletedProcess(args, 0, out, "")


def window(index, pane, path=CWD):
    return TmuxWindow(index, "paprobe", "C:\\Home\\x\\.local\\bin\\claude.exe", path, pane, "pantheon")


def make_app(tmp_path, events, windows, glyphs=None):
    cfg = config_mod.Config(state_dir=str(tmp_path))
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.events_file.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    cfg = dataclasses.replace(cfg, projects_root="C:/Home/x/Documents/Projects")
    if glyphs:
        cfg = dataclasses.replace(cfg, appearance={"glyphs": glyphs})
    app = SupervisorApp(cfg=cfg, window_source=lambda: list(windows))
    return app


def _no_sleep(app):
    app.pane.approvals._sleep = lambda s: None


def _cells(app, i=0):
    from textual.widgets import DataTable
    table = app.query_one("#agents", DataTable)
    return [c.plain if hasattr(c, "plain") else str(c) for c in table.get_row_at(i)]


def _status(app):
    from textual.widgets import Static
    return str(app.query_one("#status", Static).content)


def _detail(app):
    from textual.widgets import Static
    return str(app.query_one("#detail", Static).content)


# --------------------------------------------------------------------------- the pure rules


def test_the_live_screens_read_as_what_they_are():
    b = ap.read_screen(BASH)
    assert b.kind == "permission" and b.title == "Bash command" and b.tool == "Bash"
    assert b.body[0] == PROBE_CMD and b.yes_key == "1" and b.no_key == "4"
    long = ap.read_screen(BASH_LONG)
    assert long.body[0].startswith("cd /tmp/pw/paprobe") and long.no_key == "4"
    w = ap.read_screen(WRITE)
    assert w.tool == "Write" and w.body[0] == "notes\\plan.md" and w.no_key == "3"
    assert "╌" not in "".join(w.body)
    assert ap.read_screen(ASK).kind == "question"
    assert ap.read_screen(IDLE) is None and ap.read_screen(TRUST) is None


def test_the_option_never_chosen_is_yes_and_dont_ask_again():
    for screen in (BASH, BASH_LONG, WRITE):
        p = ap.read_screen(screen)
        chosen = dict(p.options)[p.yes_key]
        assert chosen == "Yes"


def test_pending_follows_the_hook_then_the_notification_then_clears():
    from pantheon.models import Event
    evs = [Event.from_dict(start_event("s", "%0", ago=30)),
           Event.from_dict(request_event("s", "%0", "Bash", {"command": PROBE_CMD}, ago=10)),
           Event.from_dict(notification_event("s", "%0", ago=4))]
    req = ap.pending(evs)["s"]
    assert req.known and req.tool == "Bash" and req.message == "Claude needs your permission"
    evs.append(Event.from_dict({"ts": iso(1), "event": "PostToolUse", "session_id": "s",
                                "tool_name": "Read"}))
    assert "s" in ap.pending(evs)           # a different tool finishing does not clear it
    evs.append(Event.from_dict({"ts": iso(0), "event": "PostToolUse", "session_id": "s",
                                "tool_name": "Bash"}))
    assert "s" not in ap.pending(evs)


@pytest.mark.parametrize("tool,ti,word", [
    ("Bash", {"command": "git push origin main"}, "publish"),
    ("Bash", {"command": "rm -rf build"}, "delete"),
    ("Bash", {"command": "cat /etc/hosts"}, "outside"),
    ("Bash", {"command": "cd .. && ls"}, "outside"),
    ("Bash", {"command": "cat CLAUDE.md"}, "settings"),
    ("Bash", {"command": "curl -s https://example.com"}, "network"),
    ("Bash", {"command": "npm run build 2>/dev/null"}, "run"),
    ("Edit", {"file_path": CWD + "\\src\\a.py"}, "edit"),
    ("Write", {"file_path": CWD + "\\CHECKPOINT.md"}, "settings"),
    ("Read", {"file_path": "C:\\Home\\x\\.claude\\settings.json"}, "outside"),
    ("Read", {"file_path": CWD + "\\notes.md"}, "read"),
    ("WebFetch", {"url": "https://docs.python.org/3/"}, "network"),
    ("AskUserQuestion", {"questions": [{"question": "Tea or coffee?"}]}, "question"),
])
def test_the_risk_word_is_the_heaviest_that_applies(tool, ti, word):
    assert ap.risk(tool, ti, CWD) == word


def test_a_command_with_several_parts_says_so_up_front():
    s = ap.summarize(ap.Request("s", "Bash", {"command": "cd app && npm ci && npm run build"}, "C:/x"))
    assert s.target.startswith("3 commands: cd app && npm ci")


def test_11_every_line_fits_80_and_ascii_mode_is_pure_ascii():
    """Acceptance 11: 80 characters or fewer; `glyphs = "ascii"` shows `!!`/`!` and no
    non-ASCII byte; the risk word is always written out (colour is never the only signal)."""
    inputs = [("Bash", {"command": LONG_CMD}), ("Bash", {"command": "git push"}),
              ("Edit", {"file_path": CWD + "\\" + "deep\\" * 40 + "f.py"}),
              ("AskUserQuestion", {"questions": [{"question": "Q? " * 60}]}),
              ("mcp__github__create_issue", {"title": "é" * 200}),
              ("Bash", {"command": "echo ⏎ \n" * 30})]
    for tool, ti in inputs:
        s = ap.summarize(ap.Request("s", tool, ti, CWD))
        for ascii_only in (False, True):
            text, cut = ap.line(s, ascii_only=ascii_only)
            assert len(text) <= 80
            assert s.risk in text
            if ascii_only:
                text.encode("ascii")
                assert text.startswith(("!! ", "! "))
            if cut:
                assert text.endswith(f"+{cut}")


def test_a_cut_line_says_how_much_is_hidden_and_hides_nothing_else():
    text, cut = ap.clip("x" * 100, 80)
    assert len(text) == 80 and text.endswith(f"… +{cut}") and text.count("x") + cut == 100


# --------------------------------------------------------------------------- the deck


def _one_bash_app(tmp_path, monkeypatch, cmd=PROBE_CMD, screen=BASH, after=None, width=200,
                  glyphs=None, ti=None):
    panes = Panes({"%0": screen}, after or {})
    monkeypatch.setattr(tmuxctl, "run", panes.run)
    events = [start_event("sess-a", "%0"),
              request_event("sess-a", "%0", "Bash", ti or {"command": cmd, "description": "Write probe string to probe1.txt"}),
              notification_event("sess-a", "%0")]
    app = make_app(tmp_path, events, [window(3, "%0")], glyphs=glyphs)
    return app, panes


def test_1_a_bash_prompt_shows_its_exact_command_and_v_shows_all_of_it(tmp_path, monkeypatch):
    app, panes = _one_bash_app(tmp_path, monkeypatch)
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.pause()
            seen["cells"] = _cells(app)
            seen["detail"] = _detail(app)
            await pilot.press("v")
            await pilot.pause()
            seen["view"] = str(app.screen.query_one("#request-text").content)
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(drive())
    assert any("run · Bash · echo" in c and "… +" in c for c in seen["cells"])
    assert "▲ run · Bash · " + PROBE_CMD in seen["detail"]
    assert "a allow once · d deny · v see all of it · j go to the window" in seen["detail"]
    assert "    " + PROBE_CMD in seen["view"]
    assert "Claude says: Write probe string to probe1.txt" in seen["view"]
    assert seen["view"].index(PROBE_CMD) < seen["view"].index("Claude says:")
    assert panes.keys == []                       # looking never types anything


def test_2_a_types_the_plain_yes_once_and_the_row_goes_back_to_working(tmp_path, monkeypatch):
    app, panes = _one_bash_app(tmp_path, monkeypatch, after={("%0", "1"): RUNNING})
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            _no_sleep(app)
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            seen["status"] = _status(app)
            seen["row"] = app.pane.rows[0].status

    asyncio.run(drive())
    assert panes.keys == [("%0", "1")]            # one key, the plain "1. Yes"
    assert "allowed once" in seen["status"]
    assert seen["row"] is AgentStatus.WORKING


def test_3_d_types_the_no_option_and_nothing_runs(tmp_path, monkeypatch):
    app, panes = _one_bash_app(tmp_path, monkeypatch, after={("%0", "4"): INTERRUPTED})
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            _no_sleep(app)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            seen["row"] = app.pane.rows[0].status

    asyncio.run(drive())
    assert panes.keys == [("%0", "4")]            # "4. No" -- never "1"
    assert seen["row"] is AgentStatus.WAITING_INPUT


def test_4_a_cut_line_opens_the_full_view_first_and_the_second_a_allows(tmp_path, monkeypatch):
    app, panes = _one_bash_app(tmp_path, monkeypatch, cmd=LONG_CMD, screen=BASH_LONG,
                               after={("%0", "1"): RUNNING})
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            _no_sleep(app)
            await pilot.pause()
            seen["detail"] = _detail(app)
            await pilot.press("a")
            await pilot.pause()
            seen["keys_after_first"] = list(panes.keys)
            seen["view"] = str(app.screen.query_one("#request-text").content)
            await pilot.press("a")
            await pilot.pause()

    asyncio.run(drive())
    assert "… +" in seen["detail"] and "a see all of it first" in seen["detail"]
    assert seen["keys_after_first"] == []         # the first `a` approved nothing
    assert LONG_CMD.split(" && ")[1] in seen["view"]
    assert panes.keys == [("%0", "1")]


def test_4b_a_request_known_only_from_the_screen_is_never_one_press(tmp_path, monkeypatch):
    """Until the PermissionRequest hook entry is registered only the Notification arrives: the
    row says the full request is not known, and `a` shows the dialog as the window draws it."""
    panes = Panes({"%0": BASH}, {("%0", "1"): RUNNING})
    monkeypatch.setattr(tmuxctl, "run", panes.run)
    app = make_app(tmp_path, [start_event("s", "%0"), notification_event("s", "%0")], [window(3, "%0")])
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            _no_sleep(app)
            await pilot.pause()
            seen["detail"] = _detail(app)
            await pilot.press("a")
            await pilot.pause()
            seen["first"] = list(panes.keys)
            seen["view"] = str(app.screen.query_one("#request-text").content)
            await pilot.press("a")
            await pilot.pause()

    asyncio.run(drive())
    assert "▲ run · Bash · " + PROBE_CMD in seen["detail"]
    assert "full request not known" in seen["detail"]
    assert seen["first"] == []
    assert "hook is not registered" in seen["view"] and PROBE_CMD in seen["view"]
    assert panes.keys == [("%0", "1")]


def test_5_a_never_trusted_folder_shows_trust_and_a_gets_past_it(tmp_path, monkeypatch):
    trusted = TRUST.replace(" > No, exit", "   No, exit").replace("   Yes, I trust this folder",
                                                                    " > Yes, I trust this folder")
    panes = Panes({"%9": TRUST}, {("%9", "Down"): trusted, ("%9", "Enter"): IDLE},
                  aliases={"pantheon:4": "%9"})
    monkeypatch.setattr(tmuxctl, "run", panes.run)
    w = TmuxWindow(4, "trustprobe_a1", "claude.exe", "C:/msys64/tmp/trustprobe_a1", "%9", "pantheon")
    app = make_app(tmp_path, [], [w])
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            _no_sleep(app)
            await pilot.pause()
            seen["detail"] = _detail(app)
            await pilot.press("a")
            await pilot.pause()

    asyncio.run(drive())
    assert "▲ trust · folder · trustprobe_a1" in seen["detail"]
    assert "a trust it · d exit" in seen["detail"]
    assert [k for _t, k in panes.keys] == ["Down", "Enter"]   # onto "Yes", never a bare Enter


def test_6_answered_in_the_window_clears_and_a_later_a_sends_nothing(tmp_path, monkeypatch):
    app, panes = _one_bash_app(tmp_path, monkeypatch)
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            _no_sleep(app)
            await pilot.pause()
            panes.screens["%0"] = RUNNING          # The user pressed 1 in the window itself
            await pilot.press("a")                 # before the deck's next read
            await pilot.pause()
            seen["status"] = _status(app)
            app.pane.refresh_rows()
            await pilot.pause()
            seen["shown"] = dict(app.pane.approvals.shown)
            seen["row"] = app.pane.rows[0].status
            await pilot.press("a")
            await pilot.pause()
            seen["status2"] = _status(app)

    asyncio.run(drive())
    assert panes.keys == []
    assert ap.ANSWERED_ELSEWHERE in seen["status"]
    assert seen["shown"] == {} and seen["row"] is AgentStatus.WORKING
    assert approvals_mod.NOTHING_WAITING in seen["status2"]


def test_6b_a_different_prompt_on_screen_sends_nothing(tmp_path, monkeypatch):
    app, panes = _one_bash_app(tmp_path, monkeypatch)
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            _no_sleep(app)
            await pilot.pause()
            panes.screens["%0"] = BASH.replace(PROBE_CMD, "git push --force origin main")
            await pilot.press("a")
            await pilot.pause()
            seen["status"] = _status(app)

    asyncio.run(drive())
    assert panes.keys == [] and ap.PROMPT_CHANGED in seen["status"]


def test_7_two_waiting_sessions_a_changes_only_the_selected_window(tmp_path, monkeypatch):
    panes = Panes({"%0": BASH, "%5": BASH}, {("%0", "1"): RUNNING, ("%5", "1"): RUNNING})
    monkeypatch.setattr(tmuxctl, "run", panes.run)
    other = "C:\\msys64\\tmp\\pw\\other"
    events = [start_event("a", "%0"), request_event("a", "%0", "Bash", {"command": PROBE_CMD}, ago=20),
              start_event("b", "%5", cwd=other),
              request_event("b", "%5", "Bash", {"command": PROBE_CMD}, cwd=other, ago=5)]
    app = make_app(tmp_path, events, [window(3, "%0"), window(5, "%5", path=other)])

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            _no_sleep(app)
            await pilot.pause()
            first = app.pane.rows[0]
            await pilot.press("a")
            await pilot.pause()
            return first

    first = asyncio.run(drive())
    target = "%5" if first.session_id == "b" else "%0"
    assert panes.keys == [(target, "1")]


def test_8_a_question_shows_question_and_a_d_type_nothing(tmp_path, monkeypatch):
    panes = Panes({"%0": ASK})
    monkeypatch.setattr(tmuxctl, "run", panes.run)
    ti = {"questions": [{"question": "Tea or coffee?", "header": "Beverage", "multiSelect": False,
                         "options": [{"label": "Tea", "description": "Tea"}]}]}
    app = make_app(tmp_path, [start_event("s", "%0"), request_event("s", "%0", "AskUserQuestion", ti)],
                   [window(3, "%0")])
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.pause()
            seen["detail"] = _detail(app)
            await pilot.press("a")
            await pilot.pause()
            seen["a"] = _status(app)
            await pilot.press("d")
            await pilot.pause()
            seen["d"] = _status(app)

    asyncio.run(drive())
    assert "▲ question · AskUserQuestion · Tea or coffee?" in seen["detail"]
    assert panes.keys == []
    assert ap.ANSWER_THERE in seen["a"] and ap.ANSWER_THERE in seen["d"]


def test_9_a_desktop_app_prompt_is_read_only(tmp_path, monkeypatch):
    panes = Panes({})
    monkeypatch.setattr(tmuxctl, "run", panes.run)
    app = make_app(tmp_path, [start_event("s", None, ago=30),
                              request_event("s", None, "Bash", {"command": "git push origin main"})], [])
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.pause()
            seen["detail"] = _detail(app)
            await pilot.press("a")
            await pilot.pause()
            seen["status"] = _status(app)

    asyncio.run(drive())
    assert "!! publish · Bash · git push origin main" in seen["detail"]
    assert panes.keys == [] and ap.ANSWER_IN_DESKTOP in seen["status"]


POWERSHELL = shutil.which("powershell.exe") or "/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


@pytest.mark.skipif(not Path(POWERSHELL).exists(), reason="Windows PowerShell not found")
def test_10_the_hook_records_the_request_and_decides_nothing(tmp_path):
    """Acceptance 10's mechanism: the hook prints nothing and exits 0, so Claude Code keeps its own
    dialog on screen whether or not the deck is running. Runs a COPY of the hook, so the line lands
    in this test's folder, never in the real `state/agents/events.jsonl`."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    src = Path(__file__).resolve().parent.parent / "hooks" / "pantheon_event.ps1"
    shutil.copy(src, hooks / "pantheon_event.ps1")
    payload = {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": CWD,
               "tool_name": "Bash", "permission_mode": "default",
               "tool_input": {"command": "git push origin main > out.txt", "description": "Push"}}
    script = subprocess.run(["cygpath", "-w", str(hooks / "pantheon_event.ps1")],
                            capture_output=True, text=True).stdout.strip() or str(hooks / "pantheon_event.ps1")
    cp = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                        input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip() == ""
    line = json.loads((tmp_path / "state" / "agents" / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["event"] == "PermissionRequest" and line["tool_input"] == payload["tool_input"]


def test_11_ascii_mode_on_the_deck_has_no_non_ascii_in_the_request(tmp_path, monkeypatch):
    app, _panes = _one_bash_app(tmp_path, monkeypatch, ti={"command": "git push origin main"},
                                glyphs="ascii")
    seen = {}

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.pause()
            seen["line"] = app.pane.approvals.shown["sess-a"].line(80, True)[0]
            seen["detail"] = _detail(app)

    asyncio.run(drive())
    assert seen["line"] == "!! publish - Bash - git push origin main"
    assert seen["line"] in seen["detail"]


def test_12_at_80_columns_the_request_is_in_the_footer_and_the_buttons(tmp_path, monkeypatch):
    app, _panes = _one_bash_app(tmp_path, monkeypatch, ti={"command": "git push origin main"})
    seen = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            from pantheon.widgets.toolbar import RowToolbar
            from textual.widgets import Button
            seen["status"] = _status(app)
            seen["buttons"] = [str(b.label) for b in app.query_one(RowToolbar).query(Button)]

    asyncio.run(drive())
    assert "!! publish · Bash · git push origin main" in seen["status"]
    assert any(b.startswith("Allow once") for b in seen["buttons"])
    assert any(b.startswith("Deny") for b in seen["buttons"])
