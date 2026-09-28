"""`pantheon/dialogs.py`: spotting and answering Claude Code's startup questions from screen text.
The two fixture screens were captured from Claude Code 2.1.283 in a throwaway tmux server on
2026-09-26 (an untrusted temp folder; a temp folder with a one-server `.mcp.json`)."""
from __future__ import annotations

from pathlib import Path

from pantheon import dialogs

FIXTURES = Path(__file__).parent / "fixtures" / "dialogs"
TRUST = (FIXTURES / "trust_folder_2.1.283.txt").read_text(encoding="utf-8")
MCP = (FIXTURES / "mcp_server_2.1.283.txt").read_text(encoding="utf-8")

NORMAL = """\
 ▐▛███▛█   Claude Code v2.1.283
▝▜██████▀  Opus 5.5 with xhigh effort · Claude Max
 ▝▝   ▝▝   C:\\msys64\\tmp\\trustprobe_a1 · /rc
────────────────────────────────────────────────────────────────────────────────
> Try "fix typecheck errors"
────────────────────────────────────────────────────────────────────────────────
  5h 68% · wk 45% · ctx ? · $0.00 · Opus 5.5
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""

# A live session whose conversation QUOTES the dialog (e.g. one reviewing this very module): the
# words are on screen, but above the prompt box, so they are conversation, not a dialog.
QUOTED = """\
● The trust screen reads "Accessing workspace:" and offers "Yes, I trust this folder".
  New MCP server found in this project: probe-echo
────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
  5h 61% · wk 44% · ctx 66% · $126.66 · Opus 5.5
"""


def test_the_real_trust_screen_is_a_trust_dialog_naming_the_folder():
    d = dialogs.detect(TRUST, project="ignored")
    assert d is not None and d.kind == "trust_folder" and d.is_trust
    assert d.reason == "asking whether to trust trustprobe_a1"
    assert d.folder == "C:\\msys64\\tmp\\trustprobe_a1"
    assert d.auto_answer and d.yes_option == "yes, i trust this folder"
    assert "Quick safety check" in d.text and "Yes, I trust this folder" in d.text


def test_the_real_mcp_screen_is_surfaced_not_auto_answered():
    d = dialogs.detect(MCP, project="loom-os")
    assert d is not None and d.kind == "mcp_server"
    assert d.reason == "asking whether to use a new MCP server in loom-os"
    assert not d.auto_answer and d.yes_option == ""


def test_a_normal_screen_and_a_quoting_conversation_are_not_dialogs():
    assert dialogs.detect(NORMAL) is None
    assert dialogs.detect(QUOTED) is None
    assert dialogs.detect("") is None


def test_the_binary_only_signatures_match_their_own_words():
    bypass = ("─" * 40 + "\n WARNING: Claude Code running in Bypass Permissions mode\n"
              " By proceeding, you accept all responsibility\n > No, exit\n   Yes, I accept\n")
    d = dialogs.detect(bypass, project="scratch")
    assert d.kind == "bypass_permissions" and not d.auto_answer
    assert d.reason == "asking you to accept bypass-permissions mode in scratch"


def test_the_options_block_and_cursor_are_read_from_the_real_screens():
    opts, sel = dialogs._options(dialogs.dialog_region(TRUST))
    assert opts == ["no, exit", "yes, i trust this folder"] and sel == 0
    opts, sel = dialogs._options(dialogs.dialog_region(MCP))
    assert len(opts) == 3 and sel == 2


class FakePane:
    """A pane showing the real trust screen that reacts to Down/Up/Enter/Escape like Claude does."""

    def __init__(self, screen: str = TRUST, swallow_first: bool = False):
        self.screen = screen
        self.keys: list[str] = []
        self.swallow_first = swallow_first

    def capture(self, target, tmux=None):
        return self.screen

    def press(self, target, key, tmux=None):
        self.keys.append(key)
        if self.swallow_first:
            self.swallow_first = False
            return True
        if key == "Down":
            self.screen = self.screen.replace(" > No, exit", "   No, exit").replace(
                "   Yes, I trust this folder", " > Yes, I trust this folder")
        elif key == "Up":
            self.screen = TRUST
        elif key == "Enter":
            if " > Yes, I trust this folder" in self.screen:
                self.screen = NORMAL
            else:
                self.screen = "bash-5.3$ "
        elif key == "Escape":
            self.screen = "bash-5.3$ "
        return True


def _answer(pane, yes=True, dialog=None):
    dialog = dialog or dialogs.detect(pane.screen)
    return dialogs.answer("t:1", dialog, yes, sleep=lambda s: None,
                          capture=pane.capture, press=pane.press)


def test_yes_moves_the_cursor_onto_yes_checks_it_then_presses_enter():
    pane = FakePane()
    ok, why = _answer(pane)
    assert ok, why
    assert pane.keys == ["Down", "Enter"]          # never a bare Enter on "No, exit"
    assert pane.screen == NORMAL


def test_a_swallowed_first_key_is_retried_once_before_enter():
    pane = FakePane(swallow_first=True)
    ok, why = _answer(pane)
    assert ok, why
    assert pane.keys == ["Down", "Down", "Enter"]


def test_no_is_the_dialogs_own_escape():
    pane = FakePane()
    ok, _ = _answer(pane, yes=False)
    assert ok and pane.keys == ["Escape"]


def test_nothing_is_typed_when_the_question_is_already_gone():
    pane = FakePane()
    d = dialogs.detect(pane.screen)
    pane.screen = NORMAL
    ok, why = _answer(pane, dialog=d)
    assert not ok and pane.keys == [] and "no longer" in why


def test_a_dialog_with_no_one_key_yes_is_never_answered_yes():
    pane = FakePane(screen=MCP)
    ok, why = _answer(pane)
    assert not ok and pane.keys == []
