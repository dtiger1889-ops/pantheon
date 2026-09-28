"""Startup dialogs: the questions Claude Code asks on screen BEFORE a session starts.

Why this exists: a `claude` started in a tmux window can
stop at one of these questions and sit there. No hook fires before the session starts and Remote
Control is not connected yet, so neither Claude Desktop nor claude.ai shows anything; the window
just looks dead. The only place the question exists is the pane's screen, so this module reads
the screen text (`tmux capture-pane`) and matches it against a table of known dialogs.

What was actually seen:
- folder trust: "Accessing workspace: <folder> ... Quick safety check: Is this a project you
  created or one you trust?" with `> No, exit` / `Yes, I trust this folder`. The cursor starts on
  `No, exit`, so a bare Enter would CLOSE Claude; Down then Enter answers yes.
- a new MCP server in the folder's `.mcp.json` (shows right after the trust answer): three
  options, the cursor starts on "Continue without using this MCP server".
The other rows of `SIGNATURES` come from the installed binary's own text, not a live screen
(bypass-permissions warning, several MCP servers at once, managed-settings approval, a broken
settings file, a custom API key); `seen_live` says which is which.

Only the folder-trust dialog may ever be answered without the user pressing a key, and only for a
session Pantheon itself started in a folder he picked (`providers/claude.py`). Everything else is
surfaced (supervisor row + toast), never answered.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Callable, Optional

from . import tmuxctl

# A horizontal rule Claude Code draws above its prompt box and above a dialog. The dialog is
# whatever sits BELOW the last rule on screen; text further up is the conversation, which can
# quote these very phrases (a session reviewing this file would) and must never count.
_RULE_RE = re.compile(r"^[\s│╭╰├]*[─━═]{20,}")
_CURSORS = (">", "❯", "›")
_BOX_CHARS = "│╭╮╰╯"


@dataclass(frozen=True)
class Signature:
    kind: str                  # stable id, e.g. "trust_folder"
    needles: tuple[str, ...]   # every one must appear (lower-cased, spaces collapsed) in the dialog
    reason: str                # plain words for the row; `{project}` is filled in
    yes_option: str = ""       # the option text that means yes, lower-cased; "" = no one-key yes
    no_effect: str = ""        # what Esc (the dialog's own cancel) does, in plain words
    auto_answer: bool = False  # only folder trust, and only on a Pantheon launch
    seen_live: bool = False    # True = matched against a real captured screen


SIGNATURES: tuple[Signature, ...] = (
    Signature("trust_folder", ("yes, i trust this folder",),
              "asking whether to trust {project}", yes_option="yes, i trust this folder",
              no_effect="closes Claude in that window", auto_answer=True, seen_live=True),
    # The same question in older Claude Code builds ("Do you trust the files in this folder?").
    Signature("trust_folder", ("do you trust the files in this folder",),
              "asking whether to trust {project}", yes_option="yes, proceed",
              no_effect="closes Claude in that window", auto_answer=True),
    Signature("mcp_server", ("new mcp server found in this project",),
              "asking whether to use a new MCP server in {project}",
              no_effect="starts without that MCP server", seen_live=True),
    Signature("mcp_servers", ("new mcp servers found in this project",),
              "asking which new MCP servers to use in {project}",
              no_effect="starts without those MCP servers"),
    Signature("bypass_permissions", ("running in bypass permissions mode", "yes, i accept"),
              "asking you to accept bypass-permissions mode in {project}",
              no_effect="closes Claude in that window"),
    Signature("managed_settings", ("yes, i trust these settings",),
              "asking you to approve managed settings in {project}",
              no_effect="closes Claude in that window"),
    Signature("settings_error", ("continue without these settings",),
              "stopped on a broken settings file in {project}"),
    Signature("settings_error", ("settings error", "exit and fix manually"),
              "stopped on a broken settings file in {project}"),
    Signature("api_key", ("detected a custom api key",),
              "asking whether to use a custom API key in {project}"),
)


@dataclass(frozen=True)
class Dialog:
    kind: str
    reason: str                # e.g. "asking whether to trust loom-os"
    text: str                  # the dialog as it reads on screen (trimmed), for the deck's modal
    yes_option: str = ""
    no_effect: str = ""
    auto_answer: bool = False
    folder: Optional[str] = None   # the folder the trust dialog names, when it names one

    @property
    def is_trust(self) -> bool:
        return self.kind == "trust_folder"


def _clean(line: str) -> str:
    return line.strip().strip(_BOX_CHARS).strip()


def dialog_region(screen: str) -> list[str]:
    """The lines below the last horizontal rule, trailing blanks dropped. A normal Claude screen
    ends rule / prompt / rule / status line, so this is just the status line there; while a
    startup dialog is up it is the dialog itself. No rule at all -> the whole screen."""
    lines = (screen or "").splitlines()
    last_rule = -1
    for i, line in enumerate(lines):
        if _RULE_RE.match(line):
            last_rule = i
    region = lines[last_rule + 1:]
    while region and not region[-1].strip():
        region.pop()
    return region


def _flat(lines: list[str]) -> str:
    return " ".join(" ".join(_clean(l).lower().split()) for l in lines if l.strip())


def _folder_after(lines: list[str], marker: str) -> Optional[str]:
    """The first non-blank line after the one containing `marker` (the trust dialog prints the
    folder there)."""
    for i, line in enumerate(lines):
        if marker in line.lower():
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    return _clean(nxt)
            return None
    return None


def _short_name(folder: Optional[str]) -> Optional[str]:
    if not folder:
        return None
    name = PureWindowsPath(folder.replace("/", "\\")).name
    return name or folder


def detect(screen: str, project: Optional[str] = None) -> Optional[Dialog]:
    """The startup dialog on this screen, or None. `project` names the folder in the reason when
    the dialog does not print one."""
    region = dialog_region(screen)
    if not region:
        return None
    flat = _flat(region)
    for sig in SIGNATURES:
        if all(n in flat for n in sig.needles):
            folder = _folder_after(region, "accessing workspace:") if sig.kind == "trust_folder" else None
            name = _short_name(folder) or project or "this folder"
            text = "\n".join(_clean(l) for l in region).strip("\n")
            return Dialog(sig.kind, sig.reason.format(project=name), text, sig.yes_option,
                          sig.no_effect, sig.auto_answer, folder)
    return None


# --------------------------------------------------------------------------- the phone link


# Remote Control (the phone / Claude app link) can die with the session still running: live
# 2026-09-27, the Assistant showed `Remote Control disconnected — this session was ended or
# archived from another device or app (code 4090)` above its prompt and its status line ended
# `/rc failed`, while the deck still said `idle`. Claude Code 2.1.283's own words.
RC_FAILED = "/rc failed"
RC_DISCONNECTED = "remote control disconnected"
RC_ACTIVE = "/remote-control is active"
RC_MESSAGE_LINES = 12    # how far above the prompt box a disconnect / active notice still counts


def _rules(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if _RULE_RE.match(line)]


def _notice(line: str) -> str:
    """`lost` / `active` when this line IS Claude Code's own Remote Control notice (it starts the
    line, after the `⎿` / box marks), else "". Text in the middle of a line -- the user talking
    about the phone link with the Assistant -- never counts."""
    text = " ".join(_clean(line).lstrip("⎿·•* ").lower().split())
    if text.startswith(RC_DISCONNECTED):
        return "lost"
    if text.startswith(RC_ACTIVE):
        return "active"
    return ""


def link_lost(screen: str) -> bool:
    """The screen says the Remote Control link is down: `/rc failed` in the status line under
    the prompt box, or, among the last few lines above the prompt box, Claude Code's
    `Remote Control disconnected ...` notice with no `/remote-control is active` after it (a
    reconnect prints that line below the old notice)."""
    lines = (screen or "").splitlines()
    rules = _rules(lines)
    if len(rules) >= 2 and RC_FAILED in _flat(lines[rules[-1] + 1:]):
        return True
    if len(rules) < 2:
        return False
    above = [l for l in lines[:rules[-2]] if l.strip()][-RC_MESSAGE_LINES:]
    last = ""
    for line in above:
        last = _notice(line) or last
    return last == "lost"


_BUSY_MARKS = ("esc to interrupt", "esc to cancel")


def idle_prompt(screen: str) -> bool:
    """Claude Code is at its prompt with nothing typed and nothing running: the last box on
    screen is the prompt (`>` then nothing, or only the dim `Try "..."` suggestion), no startup
    question is up and no spinner line says a turn is in progress. The ONLY state Pantheon ever
    types a command into."""
    lines = (screen or "").splitlines()
    rules = _rules(lines)
    if len(rules) < 2 or detect(screen) is not None:
        return False
    box = [_clean(l) for l in lines[rules[-2] + 1:rules[-1]] if l.strip()]
    if len(box) != 1 or not box[0].startswith(_CURSORS):
        return False
    typed = box[0].lstrip("".join(_CURSORS)).strip()
    if typed and not typed.startswith('Try "'):
        return False
    before = _flat([l for l in lines[:rules[-2]] if l.strip()][-4:])
    return not any(mark in before for mark in _BUSY_MARKS)


# --------------------------------------------------------------------------- the continuous scan


class Scanner:
    """Reads the screens of the rows `supervisor.state.dialog_candidates` picked, cheaply: at most
    `max_per_tick` captures per call, oldest-read first; a window already showing a dialog is
    re-read every call (so an answer clears it within a tick), any other at most once every
    `rescan_seconds`. Remembers what each window showed last, so a skipped window keeps its mark.

    `scan()` returns `{session_id: (tmux target, Dialog)}` for the rows showing a dialog now."""

    def __init__(self, capture: Callable[..., str] = tmuxctl.capture, max_per_tick: int = 4,
                 rescan_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._capture = capture
        self.max_per_tick = max_per_tick
        self.rescan_seconds = rescan_seconds
        self._clock = clock
        self._last: dict[str, float] = {}         # window key -> when its screen was last read
        self._found: dict[str, Dialog] = {}       # window key -> the dialog it showed then
        # window key -> whether that read showed the Remote Control link down. Kept after the
        # window stops being a candidate (a busy session is not re-read), so the Assistant line
        # does not flip back to fine just because it started working.
        self._link_lost: dict[str, bool] = {}

    @staticmethod
    def key(row) -> str:
        """The pane id when known (a window index is reused by the next window), else the target."""
        return row.tmux_pane or f"{row.tmux_session}:{row.window_index}"

    def scan(self, rows, tmux: Optional[str] = None) -> dict[str, tuple[str, Dialog]]:
        now = self._clock()
        live = {self.key(r): r for r in rows}
        for gone in set(self._last) - set(live):          # windows that closed or got busy
            self._last.pop(gone, None)
            self._found.pop(gone, None)
        due = [k for k in live
               if k in self._found or now - self._last.get(k, float("-inf")) >= self.rescan_seconds]
        due.sort(key=lambda k: (k not in self._found, self._last.get(k, float("-inf"))))
        for k in due[: self.max_per_tick]:
            row = live[k]
            screen = self._capture(f"{row.tmux_session}:{row.window_index}", tmux=tmux)
            self._last[k] = now
            self._link_lost[k] = link_lost(screen)
            found = detect(screen, project=row.project)
            if found is None:
                self._found.pop(k, None)
            else:
                self._found[k] = found
        return {row.session_id: (f"{row.tmux_session}:{row.window_index}", self._found[k])
                for k, row in live.items() if k in self._found}

    def link_lost(self, key: str) -> bool:
        """What the last read of this window (pane id) said about its Remote Control link."""
        return bool(key) and self._link_lost.get(key, False)

    def set_link_lost(self, key: str, lost: bool) -> None:
        """A fresh read made outside the scan (the reconnect key) updates the remembered state."""
        if key:
            self._link_lost[key] = lost


# --------------------------------------------------------------------------- answering


def _options(region: list[str]) -> tuple[list[str], int]:
    """The menu under the cursor: the block of consecutive non-blank lines that holds the line
    starting with a cursor glyph. Returns (lower-cased option texts, index of the selected one);
    ([], -1) when no cursor is on screen."""
    cleaned = [_clean(l) for l in region]
    sel = next((i for i, l in enumerate(cleaned) if l.startswith(_CURSORS)), -1)
    if sel < 0:
        return [], -1
    start = sel
    while start > 0 and cleaned[start - 1]:
        start -= 1
    end = sel
    while end + 1 < len(cleaned) and cleaned[end + 1]:
        end += 1
    block = [l.lstrip("".join(_CURSORS)).strip().lower() for l in cleaned[start:end + 1]]
    return block, sel - start


def send_key(target: str, key: str, tmux: Optional[str] = None) -> bool:
    """One named key (`Down`, `Enter`, `Escape`) into a pane. Not `-l`: these are key names."""
    return tmuxctl.run("send-keys", "-t", target, key, tmux=tmux).returncode == 0


def answer(target: str, dialog: Dialog, yes: bool, tmux: Optional[str] = None,
           sleep: Callable[[float], None] = time.sleep,
           capture: Callable[..., str] = tmuxctl.capture,
           press: Callable[..., bool] = send_key,
           settle: float = 1.0) -> tuple[bool, str]:
    """Answer the dialog the way a person would, only after a fresh screen read shows it is still
    up. Yes = move the cursor onto the dialog's own yes option, check it landed there, Enter.
    No = Esc, the dialog's own cancel. `settle` waits first because Claude Code ignores keys for
    a moment after a dialog opens (its confirm widget carries an `openedAt` input guard)."""
    if settle:
        sleep(settle)
    screen = capture(target, tmux=tmux)
    now = detect(screen)
    if now is None or now.kind != dialog.kind:
        return False, "the question is no longer on screen"
    if not yes:
        press(target, "Escape", tmux=tmux)
        return True, "answered no"
    if not dialog.yes_option:
        return False, "this question has no one-key yes; answer it in the window"
    for _attempt in range(2):
        options, selected = _options(dialog_region(screen))
        want = next((i for i, o in enumerate(options) if o.startswith(dialog.yes_option)), -1)
        if selected < 0 or want < 0:
            return False, "could not find the yes option on screen"
        if want != selected:
            key = "Down" if want > selected else "Up"
            for _ in range(abs(want - selected)):
                press(target, key, tmux=tmux)
            sleep(0.3)
            screen = capture(target, tmux=tmux)
            options, selected = _options(dialog_region(screen))
        if 0 <= selected < len(options) and options[selected].startswith(dialog.yes_option):
            press(target, "Enter", tmux=tmux)
            sleep(settle or 0.5)
            after = detect(capture(target, tmux=tmux))
            if after is not None and after.kind == dialog.kind:
                return False, "pressed yes but the question is still on screen"
            return True, "answered yes"
        sleep(settle or 0.5)       # the input guard swallowed the move; look again once
        screen = capture(target, tmux=tmux)
    return False, "the cursor would not move onto yes"
