"""Claude Code adapter: interactive sessions inside the deck's tmux, with Remote
Control when the `claude` wrapper adds it. Launch =; agent rows come from
`supervisor.state.fold` and usage from `hud.sources` directly -- this adapter only launches."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

from .. import dialogs as dialogs_mod
from .. import tmuxctl
from ..dispatch import briefing as briefing_mod
from ..models import LaunchResult

NAME = "claude"
CAPABILITIES = {"interactive_tmux"}

# What `pane_current_command` reports while Claude Code is running (it is a Node program).
RUNNING = {"node", "node.exe", "claude", "claude.exe"}
START_TIMEOUT_SECONDS = 45.0
PROMPT_TIMEOUT_SECONDS = 20.0     # no dialog seen: wait this long for the prompt, then go on
DIALOG_WATCH_SECONDS = 60.0       # hard cap on watching the screen when a startup question shows

log = logging.getLogger("pantheon.providers.claude")


# `--effort` values Claude Code 2.1.283 takes (`claude --help`); a model name is passed only when
# it is plain (letters, digits, `-`, `.`, `_`, `[`, `]`), since it is typed into a bash prompt.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
_SAFE_ARG = re.compile(r"^[A-Za-z0-9._\[\]-]+$")


def claude_command(base: str, options: Optional[dict] = None) -> str:
    """The command typed into the new window. `options["resume"]` adds `--continue`, which reopens the
    most recent Claude Code conversation in that folder -- the way back after a deck restart or
    a closed window; Remote Control reconnects to it on its own."""
    options = options or {}
    cmd = base
    resume = options.get("resume")
    if isinstance(resume, str) and resume:
        cmd += f" --resume {resume}"      # exactly that conversation
    elif resume:
        cmd += " --continue"              # the most recent one in the folder
    model = str(options.get("start_model") or "").strip()
    if model and _SAFE_ARG.match(model):
        cmd += f" --model {model}"
    effort = str(options.get("start_effort") or "").strip().lower()
    if effort in EFFORTS:
        cmd += f" --effort {effort}"
    mode = str(options.get("start_mode") or "").strip()
    if mode in PERMISSION_MODES:
        cmd += f" --permission-mode {mode}"
    name = str(options.get("name") or "").replace('"', "").strip()
    if name:
        # `-n/--name` (Claude Code 2.1.283): the session's display name, written into the
        # transcript as a `custom-title` record.
        cmd += f' --name "{name}"'
    for folder in options.get("add_dirs") or []:
        folder = str(folder).replace('"', "").strip()
        if folder:
            cmd += f' --add-dir "{folder}"'      # e.g. the Assistant's shared memory folder
    memory_dir = str(options.get("auto_memory_dir") or "").replace("'", "").replace('"', "").strip()
    if memory_dir:
        # Claude Code's built-in memory loads and saves THIS folder instead of the project's own
        # (docs: autoMemoryDirectory via --settings), so the Assistant has one store, not two.
        cmd += " --settings '" + json.dumps({"autoMemoryDirectory": memory_dir}) + "'"
    prompt_file = str(options.get("append_system_prompt_file") or "").replace('"', "").strip()
    if prompt_file:
        # A file, not the text: a paragraph typed into the window would need shell quoting.
        cmd += f' --append-system-prompt-file "{prompt_file}"'
    return cmd


def session_name(project_dir: str, briefing_path: str = "") -> str:
    """`obsidian` or `obsidian: fix the sprint base view` -- the project, plus the first line of
    the task when there is one, clipped at a word; quotes and control characters dropped."""
    project = Path(project_dir).name or "claude"
    first = ""
    try:
        if briefing_path and Path(briefing_path).is_file():
            for line in Path(briefing_path).read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip().lstrip("#").strip()
                if line:
                    first = line
                    break
    except OSError:
        first = ""
    first = "".join(ch for ch in first if ch.isprintable()).replace('"', "")
    if len(first) > 48:
        first = first[:48].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return f"{project}: {first}" if first else project


def _prompt_visible(screen: str) -> bool:
    """Claude Code's input box is on screen: a line whose first visible glyph is the prompt."""
    for line in screen.splitlines():
        stripped = line.strip().lstrip("│ ").strip()
        if stripped.startswith(">") or stripped.startswith("❯"):
            return True
    return False


class ClaudeProvider:
    name = NAME

    def __init__(self, cfg, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self._sleep = sleep

    def capabilities(self) -> set[str]:
        return set(CAPABILITIES)

    # ------------------------------------------------------------------ launch

    def launch(self, project_dir: str, briefing_path: str, interactive: bool = True,
               options: Optional[dict] = None) -> LaunchResult:
        if not interactive:
            return LaunchResult(False, message="Claude works in a tmux window here; "
                                "for a run with no screen to watch, pick Codex")
        session, tmux = self.cfg.tmux_session, self.cfg.tools.tmux
        # `options["window_name"]`: the pinned Assistant's window is always `ASSISTANT`,
        # whatever folder it works in, so the deck can find it again by name.
        name = str((options or {}).get("window_name") or "") or Path(project_dir).name[:14] or "claude"
        index = tmuxctl.new_window(session, name, project_dir, tmux=tmux, detached=True)
        if index is None:
            return LaunchResult(False, message="tmux would not open a window; is pantheon running?")
        target = f"{session}:{index}"
        options = dict(options or {})
        if not options.get("name") and not options.get("resume"):
            # Every NEW session gets a name, or the Claude app lists it as a random string
            #. A resumed
            # conversation keeps whatever name it already has.
            options["name"] = session_name(project_dir, briefing_path)
        # A tmux login shell lands in HOME and ignores -c: cd explicitly.
        tmuxctl.send_text(target, f'cd "{project_dir}"', tmux=tmux)
        tmuxctl.send_text(target, claude_command(self.cfg.tools.claude, options), tmux=tmux)
        if not tmuxctl.wait_for_command(target, RUNNING, START_TIMEOUT_SECONDS, tmux=tmux, sleep=self._sleep):
            return LaunchResult(False, "tmux", index, name,
                                message=f"claude did not start in window {index}; press j to look at it")
        # Give the screen time to draw, watching for a startup question (`pantheon/dialogs.py`).
        # Folder trust is answered here, and only here: the user picked this folder in the deck (new
        # session, dispatch, resume, hand-off), and that pick is his consent to trust it
        #. It is answered only after a
        # fresh screen read shows the question is still up, by moving onto the dialog's own
        # "Yes, I trust this folder" and pressing Enter (`dialogs.answer`). Claude Code has no flag
        # that pre-trusts a folder for an interactive session (2.1.283 `--help`: only `-p` skips
        # it), and editing ~/.claude.json races the running Claude processes that rewrite it.
        # Every other question (a new MCP server, bypass-permissions, settings) is left for the user;
        # the supervisor surfaces it on the row and toasts.
        waited = 0.0
        screen = ""
        answered = False
        limit = PROMPT_TIMEOUT_SECONDS
        while waited < limit:
            self._sleep(0.5)
            waited += 0.5
            screen = tmuxctl.capture(target, tmux=tmux)
            found = dialogs_mod.detect(screen, project=name)
            if found is not None:
                limit = max(limit, DIALOG_WATCH_SECONDS)
                if found.auto_answer and not answered:
                    ok, why = dialogs_mod.answer(target, found, True, tmux=tmux, sleep=self._sleep)
                    log.info("window %s: %s -- %s", index, found.reason, why)
                    if ok:
                        answered = True
                        continue
                return LaunchResult(False, "tmux", index, name, tmux_pane=tmuxctl.pane_id(target, tmux),
                                    message=f"window {index} is {found.reason}; "
                                            "press j, answer it, then work the task again")
            if _prompt_visible(screen):
                break
        if not briefing_path:
            # A plain new session: nothing to type, the prompt is the user's.
            return LaunchResult(True, "tmux", index, name, tmux_pane=tmuxctl.pane_id(target, tmux),
                                message=f"claude is open in window {index} ({name}); press j to go there")
        text = briefing_mod.one_line(Path(briefing_path).read_text(encoding="utf-8"))
        if not tmuxctl.send_text(target, text, enter=False, tmux=tmux):
            return LaunchResult(False, "tmux", index, name, message=f"could not type the briefing into window {index}")
        # Claude Code takes a long send-keys string as a paste and needs a beat before Enter
        # counts; an Enter sent in the same breath was swallowed and the briefing sat unsent
        #. Pause, press Enter, and press it once more if the paste is still pending.
        self._sleep(1.0)
        tmuxctl.send_text(target, "", enter=True, tmux=tmux)
        self._sleep(1.5)
        if "[pasted text" in tmuxctl.capture(target, tmux=tmux).lower():
            tmuxctl.send_text(target, "", enter=True, tmux=tmux)
        return LaunchResult(True, "tmux", index, name, tmux_pane=tmuxctl.pane_id(target, tmux),
                            message=f"claude is working in window {index} ({name})")
