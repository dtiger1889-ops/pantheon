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

    def __init__(self, screen: str = TRUST, swallow_first: bool = False, swallow: int = 0):
        self.screen = screen
        self.keys: list[str] = []
        self.swallow = swallow or (1 if swallow_first else 0)   # keys dropped before input is live
        self.reads = 0

    def capture(self, target, tmux=None):
        self.reads += 1
        return self.screen

    def press(self, target, key, tmux=None):
        self.keys.append(key)
        if self.swallow:
            self.swallow -= 1
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


def test_keys_sent_before_claude_takes_input_are_retried_until_one_lands():
    """2026-11-09, a never-trusted folder: Claude dropped the first Downs (input not live yet); the answer
    keeps pressing one step at a time, checking the cursor, and only then presses Enter."""
    pane = FakePane(swallow=5)
    ok, why = _answer(pane)
    assert ok, why
    assert pane.keys == ["Down"] * 6 + ["Enter"]
    assert pane.screen == NORMAL


def test_it_waits_for_a_still_screen_before_the_first_key():
    pane = FakePane()
    _answer(pane)
    # the fresh read, then at least STILL_SECONDS of identical reads, before the first key
    assert pane.reads >= 1 + int(dialogs.STILL_SECONDS / dialogs.POLL)


def test_a_cursor_that_never_moves_gives_up_without_enter():
    pane = FakePane(swallow=10_000)
    ok, why = _answer(pane)
    assert not ok and "would not move onto yes" in why
    assert "Enter" not in pane.keys and set(pane.keys) == {"Down"}


def test_no_never_types_anything_least_of_all_escape():
    """Esc on the trust question is "Esc to cancel", which exits Claude Code (2026-11-09)."""
    pane = FakePane()
    ok, why = _answer(pane, yes=False)
    assert not ok and pane.keys == [] and why == dialogs.NO_MESSAGE
    assert pane.screen == TRUST


def test_send_key_refuses_escape(monkeypatch):
    calls = []
    monkeypatch.setattr(dialogs.tmuxctl, "run", lambda *a, **k: calls.append(a))
    assert dialogs.send_key("t:1", "Escape") is False and calls == []


def test_no_escape_is_ever_sent_on_the_answer_path():
    import inspect
    src = inspect.getsource(dialogs.answer)
    assert "Escape" not in src


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


# A real Assistant screen shape: Claude Code's dim suggested reply sat in the prompt and
# the voice puck's request was refused as "busy". Same screen plain and with colour codes.
_RULE = "─" * 60
SUGGESTED = f"""\
● The garden note is now saved in your notes.
{_RULE}
>\xa0No, leave it for now
{_RULE}
  5h 19% · wk 19% · ctx 25% · $6.46 · Opus 5.5
"""
SUGGESTED_STYLED = SUGGESTED.replace(">\xa0No, leave it for now",
                                     "\x1b[39m>\xa0\x1b[2mNo, leave it for now\x1b[0m")
TYPED_STYLED = SUGGESTED.replace(">\xa0No, leave it for now",
                                 "\x1b[39m>\xa0No, leave it for now\x1b[0m")


def test_a_dim_suggested_reply_counts_as_an_empty_prompt():
    assert not dialogs.idle_prompt(SUGGESTED)              # without colours it looks typed
    assert dialogs.idle_prompt(SUGGESTED, styled=SUGGESTED_STYLED)


def test_text_drawn_normally_is_still_typed_text():
    assert not dialogs.idle_prompt(SUGGESTED, styled=TYPED_STYLED)
    half = SUGGESTED_STYLED.replace("\x1b[2mNo,", "No,\x1b[2m")   # only part of it dim
    assert not dialogs.idle_prompt(SUGGESTED, styled=half)


def _styled_prompt(sgr: str) -> str:
    return SUGGESTED.replace(">\xa0No, leave it for now", f"\x1b[39m>\xa0\x1b[{sgr}mNo, leave it for now\x1b[0m")


def test_a_true_colour_group_is_not_read_as_dim():
    # The 2 inside 38;2;r;g;b is the colour mode, not dim.
    assert not dialogs.idle_prompt(SUGGESTED, styled=_styled_prompt("38;2;100;100;100"))
    assert not dialogs.idle_prompt(SUGGESTED, styled=_styled_prompt("38;5;2"))
    assert dialogs.idle_prompt(SUGGESTED, styled=_styled_prompt("2;38;2;22;22;22"))   # dim, grey


def test_an_empty_sgr_parameter_resets_dim():
    # `2;;37` is dim, then reset (empty = 0), then white.
    assert not dialogs.idle_prompt(SUGGESTED, styled=_styled_prompt("2;;37"))


def test_stripping_a_styled_screen_gives_its_plain_text():
    assert dialogs.strip_styles(SUGGESTED_STYLED) == SUGGESTED
    assert dialogs.strip_styles("\x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\") == "link"


def test_a_turn_that_started_before_the_styled_read_blocks_the_paste(monkeypatch):
    # The first read shows a dim suggestion, but by the
    # styled read a turn is running; the paste must judge the newer screen, not the old one.
    from pantheon import assistant_telegram as tg
    running = SUGGESTED_STYLED.replace("● The garden note", "✻ Thinking… (esc to interrupt)\n● The garden note")
    calls = []

    class Ran:
        returncode = 0
        stdout = running

    monkeypatch.setattr(tg.tmuxctl, "run", lambda *a, **k: calls.append(a) or Ran())
    state = tg.paste_when_idle("%7", "hi", capture=lambda p, tmux=None: SUGGESTED, sleep=lambda s: None)
    assert state == "busy"
    assert all(a[0] == "capture-pane" for a in calls)


def test_the_same_suggestion_in_the_styled_read_lets_the_paste_through(monkeypatch):
    from pantheon import assistant_telegram as tg
    calls = []

    class Ran:
        returncode = 0
        stdout = SUGGESTED_STYLED

    monkeypatch.setattr(tg.tmuxctl, "run", lambda *a, **k: calls.append(a) or Ran())
    state = tg.paste_when_idle("%7", "hi", capture=lambda p, tmux=None: SUGGESTED, sleep=lambda s: None)
    assert state == "sent"
    assert [a[0] for a in calls][1:] == ["set-buffer", "paste-buffer", "send-keys"]


def test_a_styled_read_of_a_different_screen_is_ignored():
    assert not dialogs.idle_prompt(SUGGESTED, styled="\x1b[2m> x\x1b[0m\n")
