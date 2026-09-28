"""The pinned Assistant: one Claude Code session that is always
there in the deck's tmux, holding one long conversation -- Pantheon's stand-in for Claude
Desktop's Dispatch, so Desktop can stay closed.

    ensure(cfg)   the deck calls it when it starts: opens the `ASSISTANT` window when it is not
                  there, resuming the saved conversation; does nothing when it is running
    learn(cfg)    reads the conversation id off the SessionStart hook event of the Assistant's
                  pane and saves it (the id `ensure` resumes next time)
    view(...)     what the sidebar's `◆ Assistant` line shows -- no tmux call, no event read

What is known lives in `state/assistant.json`: `session_id` (the conversation to resume, None
until learned), `pane_id` (the tmux pane it runs in), `started_at` (when the deck last opened
it -- a pane id alone is not enough, tmux hands the same ids out again after a server restart),
`transcript_path` (from the hook event; how "can this still be resumed" is checked) and `folder`.

Why `--resume <id>` and never `--continue`: `--continue` reopens whatever conversation is newest
in the folder, and in the workspace root that is often a Desktop-app session, not this one.

Nothing here ever raises into the deck: every function returns what happened in plain words.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import config as config_mod
from . import eventcache as eventcache_mod
from . import tmuxctl
from . import transcripts as transcripts_mod
from .models import AgentState, AgentStatus, Event, TmuxWindow, parse_ts

WINDOW_NAME = "ASSISTANT"
LABEL = "◆ Assistant"
STATE_FILE = "assistant.json"
LOCK_FILE = "assistant.lock"
LOCK_STALE_SECONDS = 180          # a start that died without cleaning up stops blocking after this
LEARN_WAIT_SECONDS = 20.0         # after a start: how long to watch for its SessionStart event
DECK_WINDOW = 0

# The words on the sidebar line.
WORKING, IDLE, NEEDS_YOU, STARTING, NOT_RUNNING = "working", "idle", "needs you", "starting", "not running"
PHONE_OFF = "phone link off"      # running, but its Remote Control link is down (dialogs.link_lost)
RECONNECT_KEY = "L"
RECONNECT_LABEL = f"↻ reconnect the phone link ({RECONNECT_KEY})"
ATTENTION_PHONE_OFF = f"Assistant: phone link off · {RECONNECT_KEY} reconnects it"

# What a pane shows when Claude has exited and only the shell it was typed into is left.
SHELLS = {"bash", "sh", "zsh", "-bash", "dash", "pwsh", "powershell"}
# What `pane_current_command` reports while Claude Code runs (same set as providers/claude.py).
RUNNING_COMMANDS = {"node", "claude", "claude-code"}

MSG_FRESH_MISSING = ("the Assistant's saved conversation could not be found, "
                     "so it started a new one (◆ in the list)")
MSG_FRESH_FAILED = ("the Assistant's saved conversation would not reopen, "
                    "so it started a new one (◆ in the list)")
MSG_STARTED = "the Assistant is open in window {index} (◆ in the list, or A)"
MSG_RESUMED = "the Assistant is back in window {index}, same conversation (◆ in the list, or A)"
MSG_OFF = "the Assistant is switched off ([assistant] enabled = false in pantheon.toml)"
MSG_BUSY = "the ASSISTANT window is running {command}, so it was left alone"
MSG_STARTING = "the Assistant is already starting"
MSG_RC_NOT_RUNNING = "the Assistant is not running; A starts it (with its phone link on)"
MSG_RC_BUSY = "the Assistant is busy or has something typed; nothing sent - try again when it is idle"
MSG_RC_FINE = "the Assistant's phone link looks fine; nothing sent"
MSG_RC_NO_MENU = "could not see Claude's /remote-control command on screen; nothing sent"
MSG_RC_BACK = "the Assistant's phone link is back on"
MSG_RC_STILL_OFF = ("typed /remote-control into the Assistant but its screen still says the link is "
                    "off; press j to look at it")

log = logging.getLogger("pantheon.assistant")


# --------------------------------------------------------------------------- the record


def _state_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / STATE_FILE


def read_state(cfg: config_mod.Config) -> dict:
    try:
        data = json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(cfg: config_mod.Config, data: dict) -> None:
    try:
        path = _state_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.exception("could not write %s", _state_path(cfg))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _when(ts: Optional[str]) -> Optional[datetime]:
    when = parse_ts(ts)
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _command(command: Optional[str]) -> str:
    base = (command or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


# --------------------------------------------------------------------------- learning the id


def learn(cfg: config_mod.Config, events: Optional[Iterable[Event]] = None) -> Optional[str]:
    """The conversation the Assistant's pane is running, from the newest `SessionStart` hook
    event that pane wrote since the deck last opened it; saved to the record when it changed (a
    `/clear` inside the Assistant starts a new conversation, and the saved id follows it).
    Returns the id now on record (None when nothing is known yet)."""
    state = read_state(cfg)
    pane, since = state.get("pane_id"), _when(state.get("started_at"))
    if not pane or since is None:
        return state.get("session_id")
    if events is None:
        try:
            events, _errors = eventcache_mod.read_events_cached(cfg.events_file)
        except OSError:
            return state.get("session_id")
    best: Optional[Event] = None
    best_when: Optional[datetime] = None
    for e in events:
        if e.event != "SessionStart" or e.source != "claude" or e.agent_id or not e.session_id:
            continue
        if e.tmux_pane != pane:
            continue
        when = _when(e.ts)
        if when is None or when < since:
            continue
        if best_when is None or when >= best_when:
            best, best_when = e, when
    if best is None:
        return state.get("session_id")
    transcript = str(best.extra.get("transcript_path") or "")
    if best.session_id != state.get("session_id") or (transcript and transcript != state.get("transcript_path")):
        state["session_id"] = best.session_id
        if transcript:
            state["transcript_path"] = transcript.replace("\\", "/")
        write_state(cfg, state)
        log.info("the Assistant's conversation is %s", best.session_id)
    return best.session_id


def can_resume(cfg: config_mod.Config, state: dict, folder: str) -> bool:
    """Whether the saved conversation's transcript file is still on disk -- `claude --resume`
    with an id whose file is gone does not start at all."""
    sid = state.get("session_id")
    if not sid:
        return False
    recorded = state.get("transcript_path")
    if recorded and Path(recorded).is_file():
        return True
    try:
        return transcripts_mod.transcript_path(sid, folder, Path(cfg.claude_home)).is_file()
    except Exception:
        return False


# --------------------------------------------------------------------------- where it is


@dataclass(frozen=True)
class Place:
    pane_id: str
    window_index: int
    window_name: str
    command: str

    @property
    def running(self) -> bool:
        return _command(self.command) in RUNNING_COMMANDS


def locate(cfg: config_mod.Config, tmux: Optional[str] = None) -> Optional[Place]:
    """Where the Assistant is in the deck's tmux session: the recorded pane when it is still
    there (in its own window, or staged in window 0), else a window named `ASSISTANT`. None when
    neither exists or tmux does not answer."""
    tmux = tmux if tmux is not None else cfg.tools.tmux
    cp = tmuxctl.run("list-panes", "-s", "-t", cfg.tmux_session, "-F",
                     "#{pane_id}|#{window_index}|#{window_name}|#{pane_current_command}", tmux=tmux)
    if getattr(cp, "returncode", 1) != 0:
        return None
    panes: list[Place] = []
    for line in (cp.stdout or "").splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        try:
            panes.append(Place(parts[0], int(parts[1]), parts[2], parts[3]))
        except ValueError:
            continue
    recorded = read_state(cfg).get("pane_id")
    for place in panes:
        # A recorded pane counts only where the Assistant can actually be: its own ASSISTANT
        # window, or staged in window 0 beside the deck. A new tmux server numbers panes from %0
        # again, so a stale record once matched the fresh BUDGET pane (a bash wrapper), which was
        # then closed as the Assistant's "bare shell".
        if recorded and place.pane_id == recorded and (
                place.window_name == WINDOW_NAME or place.window_index == DECK_WINDOW):
            return place
    for place in panes:
        if place.window_name == WINDOW_NAME:
            return place
    return None


# --------------------------------------------------------------------------- ensure


def _lock(cfg: config_mod.Config) -> bool:
    path = Path(cfg.state_dir) / LOCK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
            path.unlink()
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return True      # a state folder that cannot hold a lock must not stop the Assistant


def _unlock(cfg: config_mod.Config) -> None:
    try:
        (Path(cfg.state_dir) / LOCK_FILE).unlink()
    except OSError:
        pass


def starting(cfg: config_mod.Config) -> bool:
    """A start is in progress (the lock is held and not stale)."""
    path = Path(cfg.state_dir) / LOCK_FILE
    try:
        return time.time() - path.stat().st_mtime <= LOCK_STALE_SECONDS
    except OSError:
        return False


def adopt(cfg: config_mod.Config, place: Place, tmux: Optional[str] = None) -> None:
    """An `ASSISTANT` window is running Claude but the record names another pane (or none): it
    was opened before the record existed, or the record was lost. Take it as the Assistant. Its
    SessionStart event was written before now, so the "since" is the tmux server's own start
    time -- a pane id is unique within one server's life, which is what the time filter is for."""
    since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    cp = tmuxctl.run("display-message", "-p", "#{start_time}", tmux=tmux)
    try:
        since = datetime.fromtimestamp(int((cp.stdout or "").strip()), timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        pass
    state = read_state(cfg)
    state.update({"pane_id": place.pane_id, "started_at": _iso(since),
                  "folder": state.get("folder") or cfg.assistant_settings().folder})
    write_state(cfg, state)
    log.info("took the running ASSISTANT window (pane %s) as the Assistant", place.pane_id)


def _default_provider(cfg: config_mod.Config):
    from .providers.claude import ClaudeProvider
    return ClaudeProvider(cfg)


def _result(action: str, message: str = "", ok: bool = True, **extra) -> dict:
    return {"ok": ok, "action": action, "message": message, **extra}


def _clear_shell_window(cfg: config_mod.Config, place: Place, tmux: Optional[str]) -> bool:
    """The Assistant's window is only a shell now (Claude was exited). Put it back in its own
    window if it was staged beside the deck, then close that shell-only window so a fresh start
    does not leave a second `ASSISTANT` window behind. Only a bare shell is ever closed."""
    if place.window_index == DECK_WINDOW:
        from . import stage as stage_mod
        released = stage_mod.release(cfg, tmux=tmux)
        if not released.get("ok") or released.get("window_index") is None:
            return False
        index = int(released["window_index"])
    else:
        if place.window_name != WINDOW_NAME:
            # Never close any window but the Assistant's own (DECK, QUEUE, BUDGET, NOTES, agents).
            log.warning("refused to close window %s (%s): it is not the Assistant's", place.window_index,
                        place.window_name)
            return False
        index = place.window_index
    return tmuxctl.kill_window(cfg.tmux_session, index, tmux=tmux)


def ensure(cfg: config_mod.Config, provider=None, tmux: Optional[str] = None,
           sleep: Callable[[float], None] = time.sleep,
           learn_wait: float = LEARN_WAIT_SECONDS,
           on_starting: Optional[Callable[[], None]] = None) -> dict:
    """Make sure the Assistant is running. Returns `{"ok", "action", "message", ...}` where
    `action` is `disabled`, `none` (tmux is not reachable; nothing tried), `running` (it already
    was), `busy` (its window runs something else; left alone), `starting` (another start holds
    the lock), `started` (first start, fresh conversation), `resumed` (the saved conversation),
    `fresh` (the saved one could not be reopened, so a new one; `message` says so) or `failed`.
    `on_starting` is called once, just before a launch begins (the deck shows `starting`)."""
    settings = cfg.assistant_settings()
    if not settings.enabled:
        return _result("disabled", MSG_OFF)
    tmux = tmux if tmux is not None else cfg.tools.tmux
    if getattr(tmuxctl.run("has-session", "-t", cfg.tmux_session, tmux=tmux), "returncode", 1) != 0:
        return _result("none")
    place = locate(cfg, tmux=tmux)
    if place is not None and place.running:
        if read_state(cfg).get("pane_id") != place.pane_id:
            adopt(cfg, place, tmux)
        learn(cfg)
        return _result("running", window_index=place.window_index, pane_id=place.pane_id)
    if place is not None and _command(place.command) not in SHELLS:
        return _result("busy", MSG_BUSY.format(command=_command(place.command) or "something else"), ok=False)
    if not _lock(cfg):
        return _result("starting", MSG_STARTING)
    try:
        if on_starting is not None:
            try:
                on_starting()
            except Exception:
                log.exception("on_starting failed")
        if place is not None and not _clear_shell_window(cfg, place, tmux):
            return _result("failed", "the ASSISTANT window holds only a shell and could not be cleared", ok=False)
        return _launch(cfg, settings.folder, provider or _default_provider(cfg), tmux, sleep, learn_wait)
    finally:
        _unlock(cfg)


def _launch(cfg: config_mod.Config, folder: str, provider, tmux: Optional[str],
            sleep: Callable[[float], None], learn_wait: float) -> dict:
    state = read_state(cfg)
    saved = state.get("session_id")
    resume = saved if saved and can_resume(cfg, state, folder) else None
    note = MSG_FRESH_MISSING if saved and not resume else ""

    started_at = _now()   # only a SessionStart written after this counts (pane ids are reused)
    settings = cfg.assistant_settings()
    result = _start(provider, folder, resume, settings, state_dir=cfg.state_dir)
    if not result.ok and resume:
        # The saved conversation did not come up. When the window fell back to a bare shell,
        # close it and start a fresh conversation instead.
        log.warning("resuming %s failed: %s", resume, result.message)
        place = locate(cfg, tmux=tmux)
        if place is not None and _command(place.command) in SHELLS:
            _clear_shell_window(cfg, place, tmux)
            started_at = _now()
            resume, note = None, MSG_FRESH_FAILED
            result = _start(provider, folder, None, settings, state_dir=cfg.state_dir)
    if not result.ok:
        return _result("failed", f"the Assistant did not start: {result.message}", ok=False,
                       window_index=result.window_index)

    record = {"session_id": resume, "pane_id": result.tmux_pane or "", "started_at": _iso(started_at),
              "folder": folder, "transcript_path": state.get("transcript_path") if resume else ""}
    write_state(cfg, record)
    waited = 0.0
    sid = learn(cfg)
    while not sid and waited < learn_wait:
        sleep(1.0)
        waited += 1.0
        sid = learn(cfg)
    index = result.window_index
    if note:
        action, message = "fresh", note
    elif resume:
        action, message = "resumed", MSG_RESUMED.format(index=index)
    else:
        action, message = "started", MSG_STARTED.format(index=index)
    return _result(action, message, window_index=index, pane_id=result.tmux_pane, session_id=sid)


# Claude Desktop is a Microsoft Store (MSIX) app: its AppData\Roaming writes are redirected into
# %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming. A process inside the app sees the plain path;
# the Assistant (tmux over ssh) sees only the redirected one.
# The real, redirected folder is searched first; it is the one Dispatch actually writes to.
_LOCAL = Path(os.environ.get("LOCALAPPDATA", f"{config_mod.HOME}/AppData/Local"))
DISPATCH_SESSION_ROOTS = [
    *sorted(_LOCAL.glob("Packages/Claude_*/LocalCache/Roaming/Claude/local-agent-mode-sessions")),
    Path(os.environ.get("APPDATA", f"{config_mod.HOME}/AppData/Roaming")) / "Claude" / "local-agent-mode-sessions",
]
MEMORY_PROMPT_FILE = "assistant_memory_prompt.md"

MEMORY_PROMPT = """You are the user's always-on Pantheon Assistant: the Claude Code stand-in for Claude Desktop's Dispatch.
You are an easy assistant for quick asks (notes, calendar, small research, small file edits), not a
consciously managed project session.

You share Dispatch's long-term memory, in {memory}. At the start of a conversation read MEMORY.md
there (an index, one line per note) and open a linked note whenever it bears on the ask. When you
learn something lasting -- how the user wants things done, a correction they made, a fact about their setup
-- save it as its own small note in that folder, in the same format as the notes already there, and
add one line for it to MEMORY.md. Update an existing note rather than writing a duplicate, and delete
a note only when the user says it is wrong.
"""


def memory_dir(settings) -> Optional[Path]:
    """The shared memory folder: `[assistant] memory`, or Dispatch's own `agent/memory` folder
    (the newest one holding a MEMORY.md), or None when switched off or not found."""
    value = (getattr(settings, "memory", "") or "").strip()
    if value.lower() in ("off", "false", "none", "no"):
        return None
    if value:
        path = Path(value)
        return path if path.is_dir() else None
    for root in DISPATCH_SESSION_ROOTS:
        try:
            found = [p.parent for p in Path(root).glob("*/*/agent/memory/MEMORY.md")]
        except OSError:
            continue
        if found:
            return max(found, key=lambda p: (p / "MEMORY.md").stat().st_mtime)
    return None


def write_memory_prompt(memory: Path, state_dir: Optional[str] = None) -> Path:
    """The instructions handed to Claude with `--append-system-prompt-file` (a file, so no shell
    quoting of a paragraph typed into the window)."""
    folder = Path(state_dir or config_mod.load().state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / MEMORY_PROMPT_FILE
    path.write_text(MEMORY_PROMPT.format(memory=memory.as_posix()), encoding="utf-8")
    return path


def _start(provider, folder: str, resume: Optional[str], settings=None, state_dir: str = ""):
    """Model and effort go on the command line at every start and resume. The name goes only on a NEW conversation: a resumed one keeps its own, and an
    app-side rename leaves no local record to check against, so Pantheon never
    renames an existing conversation."""
    options = {"window_name": WINDOW_NAME}
    if settings is not None:
        options["start_model"] = settings.model
        options["start_effort"] = settings.effort
        memory = memory_dir(settings)
        if memory is not None:
            options["add_dirs"] = [str(memory)]
            options["auto_memory_dir"] = memory.as_posix()
            options["append_system_prompt_file"] = str(write_memory_prompt(memory, state_dir))
    if resume:
        options["resume"] = resume
    elif settings is not None and settings.name:
        options["name"] = settings.name
    return provider.launch(folder, "", True, options=options)


# --------------------------------------------------------------------------- the sidebar line


@dataclass(frozen=True)
class View:
    status: str                       # one of the words above
    style: str                        # theme token for the status word
    session_id: str = ""
    pane_id: str = ""
    window_index: Optional[int] = None
    hide: frozenset = frozenset()     # session ids the sidebar must not list again
    name: str = ""                    # the conversation's own name, when it has one on disk
    link_lost: bool = False           # running, and its last screen read said Remote Control is down

    @property
    def running(self) -> bool:
        return self.window_index is not None and self.status not in (NOT_RUNNING, STARTING)


def owns(row: AgentState, state: dict) -> bool:
    """Is this folded row the Assistant? By its saved conversation id, or by its recorded pane
    while that pane is a live window (the pane alone after a server restart could be anyone's,
    and `fold` gives a reused pane only to the newest session). Every surface that must not list
    the Assistant twice -- the sidebar's project groups, THE PIT's columns table -- asks this."""
    sid, pane = state.get("session_id") or "", state.get("pane_id") or ""
    if sid and row.session_id == sid:
        return True
    return bool(pane and row.tmux_pane == pane and row.window_index is not None)


def reserved_panes(cfg: config_mod.Config) -> frozenset:
    """The Assistant's pane, which no other session may be matched to by folder (`fold`)."""
    if not cfg.assistant_settings().enabled:
        return frozenset()
    pane = read_state(cfg).get("pane_id") or ""
    return frozenset({pane}) if pane else frozenset()


def without_assistant(cfg: config_mod.Config, rows: Iterable[AgentState]) -> list[AgentState]:
    rows = list(rows or [])
    if not cfg.assistant_settings().enabled:
        return rows
    state = read_state(cfg)
    return [r for r in rows if not owns(r, state)]


_BUSY = {AgentStatus.WORKING, AgentStatus.RUNNING, AgentStatus.WINDING_DOWN}
_ENDED = {AgentStatus.GONE, AgentStatus.DONE}


def _name(cfg: config_mod.Config, state: dict) -> str:
    """The saved conversation's own name (a `custom-title` record), or ""."""
    from .dispatch import sessions as sessions_mod

    path = state.get("transcript_path") or ""
    if not path and state.get("session_id"):
        try:
            path = str(transcripts_mod.transcript_path(
                state["session_id"], state.get("folder") or cfg.assistant_settings().folder,
                Path(cfg.claude_home)))
        except Exception:
            path = ""
    if not path:
        return ""
    try:
        return sessions_mod.custom_title(Path(path)) or ""
    except Exception:
        return ""


def view(cfg: config_mod.Config, rows: Iterable[AgentState], windows: Iterable[TmuxWindow],
         is_starting: bool = False, link_lost: Optional[Callable[[str], bool]] = None) -> Optional[View]:
    """The `◆ Assistant` line, from what the deck already has (the supervisor's folded rows and
    its window list, and what the last screen read of its pane said about Remote Control --
    `link_lost(pane_id)`, no tmux call here) plus the small record file. None when the
    Assistant is switched off."""
    if not cfg.assistant_settings().enabled:
        return None
    state = read_state(cfg)
    sid, pane = state.get("session_id") or "", state.get("pane_id") or ""
    name = _name(cfg, state)
    rows = list(rows or [])
    mine = [r for r in rows if owns(r, state)]
    live = [r for r in mine if r.window_index is not None and r.status not in _ENDED]
    hide = frozenset({sid} if sid else set()) | frozenset(r.session_id for r in mine)
    window = None
    for w in windows or []:
        if (pane and w.pane_id == pane) or w.name == WINDOW_NAME:
            if _command(w.command) in RUNNING_COMMANDS:
                window = w
                break
    index = live[0].window_index if live else (window.index if window else None)
    if is_starting:
        return View(STARTING, "dim", sid, pane, index, hide, name)
    here = pane or (window.pane_id if window is not None else "")
    # The scan keys a window by its pane id, or by `session:index` when the row has no pane id.
    keys = [here] + [r.tmux_pane or f"{r.tmux_session}:{r.window_index}" for r in live[:1]]
    lost = bool(index is not None and link_lost is not None
                and any(k and link_lost(k) for k in keys))
    if live:
        row = live[0]
        # A question on its screen outranks the phone link; the phone link outranks "its turn finished".
        if row.status.needs_human and (row.status != AgentStatus.WAITING_INPUT or not lost):
            return View(NEEDS_YOU, "warning", sid or row.session_id, pane, index, hide, name, lost)
        if lost:
            return View(PHONE_OFF, "warning", sid or row.session_id, pane, index, hide, name, True)
        if row.status in _BUSY:
            return View(WORKING, "accent", sid or row.session_id, pane, index, hide, name)
        return View(IDLE, "dim", sid or row.session_id, pane, index, hide, name)
    if window is not None:
        if lost:
            return View(PHONE_OFF, "warning", sid, here, window.index, hide, name, True)
        return View(IDLE, "dim", sid, here, window.index, hide, name)
    return View(NOT_RUNNING, "dim", sid, pane, None, hide, name)


# --------------------------------------------------------------------------- reconnect


def _menu_line(screen: str, command: str) -> str:
    """The autocomplete line for `command` that Claude Code shows under a typed slash command,
    lower-cased ("" when it is not on screen)."""
    for line in (screen or "").splitlines():
        text = line.strip()
        if text.startswith(command) and len(text) > len(command) + 2:
            return " ".join(text.lower().split())
    return ""


def reconnect(cfg: config_mod.Config, tmux: Optional[str] = None,
              capture: Optional[Callable[..., str]] = None,
              sleep: Callable[[float], None] = time.sleep) -> dict:
    """Bring the Assistant's phone link (Remote Control) back: type `/remote-control` into it,
    and only after a fresh screen read shows it idle at an empty prompt with the link down.

    Claude Code's `/remote-control` TOGGLES (2.1.283: its menu line reads "Disconnect Remote
    Control" while it still counts the link as active, else "Control this session from your
    phone..."). So the command is typed, the menu line is read BEFORE Enter, and: connect ->
    Enter; disconnect (a dead link it still holds) -> Enter to drop it, a fresh idle check, then
    the same again, now as connect. Anything unexpected clears what was typed and stops.
    Returns `{"ok", "message"}`; the deck shows the message."""
    from . import dialogs as dialogs_mod

    capture = capture or tmuxctl.capture
    tmux = tmux if tmux is not None else cfg.tools.tmux
    if not cfg.assistant_settings().enabled:
        return {"ok": False, "message": MSG_OFF}
    place = locate(cfg, tmux=tmux)
    if place is None or not place.running:
        return {"ok": False, "message": MSG_RC_NOT_RUNNING}
    target = place.pane_id
    command = "/remote-control"

    def clear_typed() -> None:
        tmuxctl.run("send-keys", "-t", target, "Escape", tmux=tmux)
        tmuxctl.run("send-keys", "-t", target, "-N", str(len(command)), "BSpace", tmux=tmux)

    screen = capture(target, tmux=tmux)
    if not dialogs_mod.link_lost(screen):
        return {"ok": True, "message": MSG_RC_FINE, "link_lost": False}
    for _step in range(2):
        if not dialogs_mod.idle_prompt(screen):
            return {"ok": False, "message": MSG_RC_BUSY, "link_lost": True}
        if not tmuxctl.send_text(target, command, enter=False, tmux=tmux):
            return {"ok": False, "message": "tmux would not take the keys", "link_lost": True}
        sleep(1.0)
        menu = _menu_line(capture(target, tmux=tmux), command)
        if "control this session" in menu:
            tmuxctl.run("send-keys", "-t", target, "Enter", tmux=tmux)
            sleep(3.0)
            lost = dialogs_mod.link_lost(capture(target, tmux=tmux))
            return {"ok": not lost, "message": MSG_RC_STILL_OFF if lost else MSG_RC_BACK,
                    "link_lost": lost}
        if "disconnect" in menu:
            tmuxctl.run("send-keys", "-t", target, "Enter", tmux=tmux)   # drop the dead link
            sleep(2.0)
            screen = capture(target, tmux=tmux)
            continue
        clear_typed()
        return {"ok": False, "message": MSG_RC_NO_MENU, "link_lost": True}
    return {"ok": False, "message": MSG_RC_STILL_OFF, "link_lost": True}
