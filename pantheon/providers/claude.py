"""Claude Code adapter: interactive sessions inside the deck's tmux, with Remote
Control when the `claude` wrapper adds it. Launch =; agent rows come from
`supervisor.state.fold` and usage from `hud.sources` directly -- this adapter only launches."""
from __future__ import annotations

import itertools
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from .. import config as config_mod
from .. import dialogs as dialogs_mod
from .. import tmuxctl
from ..dispatch import briefing as briefing_mod
from ..dispatch import sessions as sessions_mod
from ..models import LaunchResult

NAME = "claude"
CAPABILITIES = {"interactive_tmux"}

# What `pane_current_command` reports while Claude Code is running (it is a Node program).
RUNNING = {"node", "node.exe", "claude", "claude.exe"}
START_TIMEOUT_SECONDS = 45.0
PROMPT_TIMEOUT_SECONDS = 20.0     # no dialog seen: wait this long for the prompt, then go on
DIALOG_WATCH_SECONDS = 60.0       # hard cap on watching the screen when a startup question shows

# Briefing delivery (2026-11-09, `docs/long-brief-loss.md`). The input box is drawn about a second
# after Claude starts, BEFORE its startup is over (SessionStart hooks, Remote Control); a brief
# typed then was lost whole, or kept only its tail. So the brief waits until the box is up, the
# new session's transcript exists (SessionStart has written to it) and the screen above and
# including the box has not changed for READY_SETTLE_SECONDS; after READY_TIMEOUT_SECONDS it
# goes in anyway and the delivery check below decides.
READY_SETTLE_SECONDS = 1.5
READY_TIMEOUT_SECONDS = 30.0
# After Enter: the transcript gains a user message, or the pane shows Claude working. A paste once
# took ~40 s to submit, so slow is not dead: wait this long before any retry.
DELIVERY_TIMEOUT_SECONDS = 60.0
RETRY_ENTER_WAIT_SECONDS = 15.0   # after the one extra Enter
RETRY_PASTE_WAIT_SECONDS = 30.0   # after the one re-paste (only when the box is empty)
# Off only under the test suite (tests/conftest.py), whose fake panes never show Claude working;
# the delivery tests turn it back on.
VERIFY_DELIVERY = True

log = logging.getLogger("pantheon.providers.claude")


# `--effort` values Claude Code 2.1.283 takes (`claude --help`); a model name is passed only when
# it is plain (letters, digits, `-`, `.`, `_`, `[`, `]`), since it is typed into a bash prompt.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
_SAFE_ARG = re.compile(r"^[A-Za-z0-9._\[\]-]+$")


_PASTE_SERIAL = itertools.count(1)


def paste_text(target: str, text: str, tmux: Optional[str] = None) -> bool:
    """Put a briefing into the pane as one bracketed paste (tmux buffer, `paste-buffer -p`), no
    Enter. `send-keys -l` typed long briefs key by key: a 2.9k brief was never submitted and
    back-to-back ~1.2k briefs arrived as their tails only (2026-10-29/30). The buffer is per call,
    so two launches at once never swap texts."""
    buffer = f"pantheon-brief-{os.getpid()}-{next(_PASTE_SERIAL)}"
    if tmuxctl.run("set-buffer", "-b", buffer, "--", text, tmux=tmux).returncode != 0:
        return False
    return tmuxctl.run("paste-buffer", "-p", "-d", "-b", buffer, "-t", target,
                       tmux=tmux).returncode == 0


def transcript_folder(claude_home: str, project_dir: str) -> Path:
    """Where Claude Code keeps this folder's transcripts: `<claude_home>/projects/<slug>`, the
    slug being the Windows path with every non-alphanumeric character turned into `-`
    (`C:\\Users\\me\\Projects\\notes_app` -> `C--Users-me-Projects-notes-app`).
    An MSYS spelling (`/c/Users/...`) is turned into the Windows one first; when the exact folder
    is missing, a folder that differs only in case is taken."""
    path = str(project_dir).replace("\\", "/")
    m = re.match(r"^/([A-Za-z])(/|$)", path)
    if m:
        path = f"{m.group(1).upper()}:/{path[3:]}"
    root = Path(claude_home) / "projects"
    folder = root / sessions_mod.slug_for(path)
    if not folder.is_dir():
        try:
            for other in root.iterdir():
                if other.name.lower() == folder.name.lower():
                    return other
        except OSError:
            pass
    return folder


def _is_user_message(record: dict) -> bool:
    """A message the user (or a paste) sent -- not a tool result, not a meta line."""
    if record.get("type") != "user" or record.get("isMeta"):
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(c, dict) and c.get("type") == "text" for c in content)
    return False


class TranscriptWatch:
    """What a launch wrote to this folder's transcripts since it started: sizes are recorded
    before Claude is typed, so a brand-new file and an appended resume both count."""

    MAX_READ = 4_000_000

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.baseline = {p: s for p, s in self._sizes().items()}

    def _sizes(self) -> dict:
        out = {}
        try:
            for p in self.folder.iterdir():
                if p.suffix == ".jsonl" and p.is_file():
                    out[p] = p.stat().st_size
        except OSError:
            pass
        return out

    def _grown(self):
        for path, size in self._sizes().items():
            start = self.baseline.get(path, 0)
            if size > start:
                yield path, start

    def started(self) -> bool:
        """The new session has written its transcript (SessionStart records)."""
        return any(True for _ in self._grown())

    def user_message(self) -> bool:
        """A user message was appended since the launch began."""
        for path, start in self._grown():
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    chunk = fh.read(self.MAX_READ)
            except OSError:
                continue
            for line in chunk.splitlines():
                if b'"user"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue      # a half-written last line
                if isinstance(record, dict) and _is_user_message(record):
                    return True
        return False


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
        # `[1m]` is a bash glob pattern: quote a name that carries one.
        cmd += f" --model '{model}'" if "[" in model else f" --model {model}"
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


def exit_suffix(root: Optional[Path] = None) -> str:
    """What follows the Claude command on the typed line: `; "<root>/bin/pane_exit" $?`, so the
    pane records Claude Code's exit code in the event log even when its SessionEnd hook never
    fires. The path is the
    one the pane's bash sees (forward slashes)."""
    script = (Path(root or config_mod.PROJECT_ROOT) / "bin" / "pane_exit").as_posix()
    return f'; "{script}" $?'


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


def _prompt_index(lines: list[str]) -> Optional[int]:
    """The index of the last line that is the input box's prompt line, or None."""
    found = None
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("│ ").strip()
        if stripped.startswith(">") or stripped.startswith("❯"):
            found = i
    return found


def _settle_view(screen: str) -> str:
    """The screen above and including the prompt line -- the status lines under the box (usage,
    mode) change on their own and are left out of the "has it settled" comparison."""
    lines = screen.splitlines()
    i = _prompt_index(lines)
    return "\n".join(lines[: i + 1]) if i is not None else screen


_PLACEHOLDER = re.compile(r'^Try ".*"$')


def _box_empty(screen: str) -> bool:
    """Nothing typed or pasted in the input box: the prompt line holds only the glyph, or Claude's
    grey `Try "..."` hint (capture-pane drops the colour, so the hint reads as text)."""
    if "[pasted text" in screen.lower():
        return False
    lines = screen.splitlines()
    i = _prompt_index(lines)
    if i is None:
        return False
    rest = lines[i].strip().lstrip("│ ").strip()[1:].strip().rstrip("│").strip()
    return not rest or bool(_PLACEHOLDER.match(rest))


def _pane_working(screen: str) -> bool:
    """Claude is on a turn: its spinner line carries `esc to interrupt`."""
    return "esc to interrupt" in screen.lower()


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
        """Start Claude in a new window and deliver the briefing. Some fresh sessions on this box
        never read a key (2026-11-09, window 8: a typed `x` was echoed by the terminal under the
        box, Enter submitted nothing). When the delivery check fails, the window this launch
        created -- the same pane id, its transcript still without a user message, the pane not
        working -- is closed and the launch runs once more. A plain session with no briefing has
        nothing to verify and is left as it is."""
        first: dict = {}
        result = self._launch_once(project_dir, briefing_path, interactive, options, first)
        if not first.get("undelivered"):
            return result
        tmux = self.cfg.tools.tmux
        pane = first.get("pane") or ""
        self._log("brief_not_delivered", project_dir, pane, attempt=1,
                  window_index=result.window_index)
        if not self._safe_to_close(first, tmux):
            log.info("window %s: brief not delivered; not closing it (not provably ours and empty)",
                     result.window_index)
            return result
        if tmuxctl.run("kill-window", "-t", pane, tmux=tmux).returncode != 0:
            return result
        self._log("dead_window_closed", project_dir, pane, attempt=1, window_index=result.window_index)
        second: dict = {}
        again = self._launch_once(project_dir, briefing_path, interactive, options, second)
        if second.get("undelivered"):
            self._log("brief_not_delivered", project_dir, second.get("pane") or "", attempt=2,
                      window_index=again.window_index, dead_pane=pane)
            again.message = f"{again.message} (tried twice)"
        elif again.ok:
            self._log("brief_delivered_on_retry", project_dir, again.tmux_pane or "", attempt=2,
                      window_index=again.window_index, dead_pane=pane)
        return again

    def _safe_to_close(self, info: dict, tmux: Optional[str]) -> bool:
        """Only the window this launch opened (its recorded pane still sits at that window), only
        when nothing was sent (no user message in the new transcript) and Claude shows no work."""
        pane, target, watch = info.get("pane"), info.get("target"), info.get("watch")
        if not pane or not target or watch is None:
            return False
        if tmuxctl.pane_id(target, tmux) != pane:
            return False
        if watch.user_message():
            return False
        return not _pane_working(tmuxctl.capture(pane, tmux=tmux))

    def _log(self, event: str, project_dir: str, pane: str, **extra) -> None:
        """One line in `state/agents/events.jsonl` per attempt, so no-input sessions can be counted."""
        try:
            from .. import events as events_mod
            from ..models import Event, utcnow_iso
            events_mod.append_event(self.cfg.events_file, Event(
                ts=utcnow_iso(), event=event, source="pantheon", cwd=project_dir,
                project=Path(project_dir).name, tmux_pane=pane or None, extra=extra))
        except (OSError, AttributeError):
            pass

    def _launch_once(self, project_dir: str, briefing_path: str, interactive: bool,
                     options: Optional[dict], info: dict) -> LaunchResult:
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
        info["target"] = target
        info["pane"] = tmuxctl.pane_id(target, tmux)     # the window this launch created
        options = dict(options or {})
        if not options.get("name") and not options.get("resume"):
            # Every NEW session gets a name, or the Claude app lists it as a random string
            #. A resumed
            # conversation keeps whatever name it already has.
            options["name"] = session_name(project_dir, briefing_path)
        # Sizes of this folder's transcripts BEFORE Claude starts, so the brief's delivery can be
        # checked against what the new session writes.
        watch = None
        if briefing_path and VERIFY_DELIVERY:
            watch = TranscriptWatch(transcript_folder(getattr(self.cfg, "claude_home", ""), project_dir))
        info["watch"] = watch
        resumed = bool(options.get("resume"))
        # A tmux login shell lands in HOME and ignores -c: cd explicitly.
        tmuxctl.send_text(target, f'cd "{project_dir}"', tmux=tmux)
        tmuxctl.send_text(target, claude_command(self.cfg.tools.claude, options) + exit_suffix(),
                          tmux=tmux)
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
        settled_view, settled_at = None, 0.0
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
            if not _prompt_visible(screen):
                settled_view = None
                continue
            if not briefing_path:
                break
            # The box is drawn before startup is over: wait for the transcript and a still screen.
            limit = max(limit, READY_TIMEOUT_SECONDS)
            view = _settle_view(screen)
            if view != settled_view:
                settled_view, settled_at = view, waited
                continue
            if watch is not None and not resumed and not watch.started():
                continue
            if waited - settled_at >= READY_SETTLE_SECONDS:
                break
        if not briefing_path:
            # A plain new session: nothing to type, the prompt is the user's.
            return LaunchResult(True, "tmux", index, name, tmux_pane=tmuxctl.pane_id(target, tmux),
                                message=f"claude is open in window {index} ({name}); press j to go there")
        log.info("window %s: pasting the briefing after %.1f s (box settled: %s)", index, waited,
                 settled_view is not None and waited - settled_at >= READY_SETTLE_SECONDS)
        text = briefing_mod.one_line(Path(briefing_path).read_text(encoding="utf-8"))
        if not self._submit(target, text, tmux):
            return LaunchResult(False, "tmux", index, name, message=f"could not paste the briefing into window {index}")
        delivered = self._await_delivery(target, watch, DELIVERY_TIMEOUT_SECONDS, tmux)
        if not delivered:
            # One more Enter: an Enter swallowed while Claude was busy leaves the paste in the box.
            log.info("window %s: no sign of the briefing after %.0f s; pressing Enter again",
                     index, DELIVERY_TIMEOUT_SECONDS)
            tmuxctl.send_text(target, "", enter=True, tmux=tmux)
            delivered = self._await_delivery(target, watch, RETRY_ENTER_WAIT_SECONDS, tmux)
        if not delivered and _box_empty(tmuxctl.capture(target, tmux=tmux)):
            # The box is empty and nothing was sent: the paste itself was lost. Paste once more --
            # into the SAME session; a second session is never started.
            log.info("window %s: box empty and nothing sent; pasting the briefing once more", index)
            if self._submit(target, text, tmux):
                delivered = self._await_delivery(target, watch, RETRY_PASTE_WAIT_SECONDS, tmux)
        pane = tmuxctl.pane_id(target, tmux)
        if not delivered:
            info["undelivered"] = True
            return LaunchResult(False, "tmux", index, name, tmux_pane=pane,
                                message=f"window {index} opened but the brief was not delivered")
        return LaunchResult(True, "tmux", index, name, tmux_pane=pane,
                            message=f"claude is working in window {index} ({name})")

    def _submit(self, target: str, text: str, tmux: Optional[str]) -> bool:
        """Paste the briefing, then Enter after a beat (an Enter in the same breath as a paste was
        swallowed), and once more if the paste still shows as pending."""
        if not paste_text(target, text, tmux=tmux):
            return False
        self._sleep(1.0)
        tmuxctl.send_text(target, "", enter=True, tmux=tmux)
        self._sleep(1.5)
        if "[pasted text" in tmuxctl.capture(target, tmux=tmux).lower():
            tmuxctl.send_text(target, "", enter=True, tmux=tmux)
        return True

    def _await_delivery(self, target: str, watch: Optional[TranscriptWatch], timeout: float,
                        tmux: Optional[str]) -> bool:
        """True once the transcript holds a new user message or the pane shows Claude working."""
        if not VERIFY_DELIVERY:
            return True
        waited = 0.0
        while True:
            if watch is not None and watch.user_message():
                return True
            if _pane_working(tmuxctl.capture(target, tmux=tmux)):
                return True
            if waited >= timeout:
                return False
            self._sleep(1.0)
            waited += 1.0
