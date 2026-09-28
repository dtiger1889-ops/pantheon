"""Turn the event log plus the live tmux window list into supervisor rows.

Pure functions only: no files, no subprocesses, no clock of its own (`now` is passed in), so the
whole state machine is unit-testable without tmux and without a running agent. The rules live in
 sections 2, 3 and 6, and 
R4 (waiting on a human is the amber Caution tier, never red; red is for a crash).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional

from .. import transcripts as transcripts_mod
from ..events import by_session, derive_project, norm_path
from ..models import AgentState, AgentStatus, Event, TmuxWindow, parse_ts

# A tmux window running one of these is a candidate agent even before any event arrives.
# Claude Code is a Node app, so `node` counts.
AGENT_COMMANDS = {"node", "node.exe", "claude", "claude.exe", "claude-code", "codex", "codex.exe"}

PRESENCE_SECONDS = 30 * 60      # a "desktop" row (no tmux pane at all) this fresh means it is present
# A row that DID record a tmux pane but no live window has it any more was in tmux and its window
# is not there any more -- the user would see the missing window immediately, so this tolerance is
# short, not the full 30 minutes.
DEAD_PANE_SECONDS = 3 * 60
HIDE_GONE_AFTER_SECONDS = 10 * 60  # a finished row stays on screen this long, then disappears
# -- a row whose last real sign of life (an event, or a fresh statusline capture,
# same as `_presence_age` already counts for presence) is older than this is not "working" any
# more, whatever its last event said, so it drops to the dim QUIET tier and stops inflating the
# command bar's `working` pill. The same age also ages a finished ("Stop"/turn done) row with no
# tmux window at all out of the amber `needs you` pill -- there is nothing the deck can jump to or
# answer for it, so it cannot need the user forever; a Stop on a row the deck CAN jump to (a real
# window) still means "needs you" with no expiry, exactly as before.
QUIET_AFTER_SECONDS = 10 * 60
MESSAGE_CHARS = 60              # how much of a notification message the "last action" column carries

_EXIT_RE = re.compile(r"exit(?:\s*code)?\s*[:=]?\s*(-?\d+)", re.I)

# Where each state sits in the list. Position is spent on "needs you":
# blocked first, then waiting, then a crash, then the ones that are fine.
SORT_RANK = {
    AgentStatus.BLOCKED_PERMISSION: 0,
    AgentStatus.RESUME_FAILED: 0,           # needs the user just as much as a permission prompt
    AgentStatus.WAITING_INPUT: 1,
    AgentStatus.FAILED: 2,
    AgentStatus.WORKING: 3,
    AgentStatus.RUNNING: 3,
    AgentStatus.WINDING_DOWN: 3,
    AgentStatus.QUIET: 3.5,          # below working, above the rest -- it WAS active recently
    AgentStatus.QUEUED: 4,
    AgentStatus.IDLE: 5,
    AgentStatus.UNKNOWN: 5,
    AgentStatus.PARKED: 5,
    AgentStatus.DONE: 5,
    AgentStatus.GONE: 6,
}


# --------------------------------------------------------------------------- small helpers


def _clip(text: Optional[str], limit: int = MESSAGE_CHARS) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t[:limit]


def _plain_words(notification_type: Optional[str]) -> str:
    """`quota_auto_resume_started` -> `auto resume started`. A non-coder reads these rows."""
    return (notification_type or "").replace("quota_", "").replace("_", " ").strip()


def _exit_code(detail: Optional[str]) -> Optional[int]:
    m = _EXIT_RE.search(detail or "")
    return int(m.group(1)) if m else None


def command_name(command: Optional[str]) -> str:
    """`C:\\Home\\x\\.local\\bin\\claude.exe` -> `claude`. tmux on this box reports the full
    path of a Windows program, not just its name."""
    base = (command or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _is_agent_window(w: TmuxWindow) -> bool:
    return command_name(w.command) in AGENT_COMMANDS


# Windows the folder fallback never lands on: the pinned Assistant's window belongs to the
# Assistant's own session (`pantheon/assistant.py`), whatever else works in the same folder.
RESERVED_WINDOW_NAMES = {"ASSISTANT"}


def match_window(
    cwd: Optional[str],
    tmux_pane: Optional[str],
    windows: Iterable[TmuxWindow],
    pantheon_session: str = "pantheon",
    provider: Optional[str] = None,
    taken: Iterable[str] = (),
) -> Optional[TmuxWindow]:
    """Find the live tmux window an agent is sitting in.

    Order matters: the pane id the hook recorded is exact, so it wins. The working
    directory is a fallback ONLY for a provider that records no pane of its own (a headless Codex
    job): a Claude session is tied to a window through its own `tmux_pane` and nothing else. On
    2026-09-27 a Desktop-app session in the workspace folder (no pane) was matched by folder to
    the pinned Assistant's window, drawn as `gone · pantheon:5`, and took the window away from
    the Assistant's own row. `taken` = pane ids that already belong to another session; the
    fallback never lands on one, nor on a reserved window (the Assistant's).
    """
    windows = list(windows)
    if tmux_pane:
        for w in windows:
            # The pane id is exact for a LIVE server, but a restarted tmux server hands out the
            # same ids again from %0: on 2026-09-02, after the user killed the server from his
            # phone, a dead loom-os session's `%2` matched the new server's usage window and
            # `j` would have landed there. So even an exact id must point at a pane that is
            # actually running an agent.
            if w.pane_id and w.pane_id == tmux_pane and _is_agent_window(w):
                return w
    if not cwd or (provider or "").lower() == "claude":
        return None
    target = norm_path(cwd)
    if not target:
        return None
    taken = set(taken or ())
    # The folder fallback only trusts a window that is actually running an agent. A shell or the
    # deck's own panes sitting in the same folder are not that agent -- on 2026-09-02 six old
    # sessions from this project were all painted as `pantheon:0`, the deck window itself.
    candidates = [w for w in windows if norm_path(w.path) == target and _is_agent_window(w)
                  and not (w.pane_id and w.pane_id in taken) and w.name not in RESERVED_WINDOW_NAMES]
    if not candidates:
        return None
    candidates.sort(key=lambda w: (0 if w.session == pantheon_session else 1, w.index))
    return candidates[0]


# --------------------------------------------------------------------------- per-source rules


def _fold_claude(e: Event, status: AgentStatus, action: str) -> tuple[AgentStatus, str]:
    """One Claude Code hook line."""
    if (e.event or "") == "PermissionRequest":
        # The session (or one of its subagents -- the prompt still shows in the session's own
        # window) is waiting on a yes/no.: the hook only reports it.
        return AgentStatus.BLOCKED_PERMISSION, f"permission: {_clip(e.tool_name, 30) or 'a tool'}"
    if e.agent_id:
        # A subagent fired this hook, not the session itself. The session is still working, so a
        # subagent finishing must never make the parent row look like it wants the user.
        return AgentStatus.WORKING, f"subagent: {_clip(e.tool_name or e.event, 20)}"

    name = e.event or ""
    if name == "SessionStart":
        return AgentStatus.WORKING, "session started"
    if name in ("PostToolUse", "PreToolUse"):
        return AgentStatus.WORKING, _clip(e.tool_name) or "running a tool"
    if name == "Stop":
        return AgentStatus.WAITING_INPUT, "turn done"
    if name == "SessionEnd":
        return AgentStatus.GONE, "session ended"
    if name == "Notification":
        nt = (e.notification_type or "").lower()
        text = _clip(e.message) or _plain_words(nt) or "notice"
        if nt.startswith("quota_auto_resume"):
            # The usage limit spoke, not the agent. Whatever it was doing, it is still doing.
            return status, f"limit: {_plain_words(nt)}"
        if nt == "permission_prompt":
            return AgentStatus.BLOCKED_PERMISSION, text
        if nt == "idle_prompt":
            return AgentStatus.IDLE, text
        if nt == "agent_needs_input":
            return AgentStatus.WAITING_INPUT, text
        return status, text
    return status, action


def _fold_codex(e: Event, status: AgentStatus, action: str) -> tuple[AgentStatus, str]:
    """One headless Codex job line."""
    name = (e.event or "").lower()
    detail = _clip(e.detail, 24)
    if name == "queued":
        return AgentStatus.QUEUED, "codex exec"
    if name == "running":
        return AgentStatus.RUNNING, "codex exec"
    if name == "done":
        code = _exit_code(e.detail)
        done_status = AgentStatus.FAILED if code not in (None, 0) else AgentStatus.DONE
        return done_status, f"codex exec {detail}".strip()
    if name == "failed":
        return AgentStatus.FAILED, f"codex exec {detail}".strip()
    return status, action


def _fold_pantheon(e: Event, status: AgentStatus, action: str) -> tuple[AgentStatus, str]:
    """A line the deck itself wrote: the user pressed a key, or the governor acted."""
    name = (e.event or "").lower()
    if name == "kill":
        return AgentStatus.GONE, "killed from the deck"
    if name == "wind_down":
        return AgentStatus.WINDING_DOWN, "asked to save and stop"
    if name == "park":
        return AgentStatus.PARKED, "parked until the limit resets"
    if name == "dispatch":
        return AgentStatus.WORKING, "started from the deck"
    if name == "resume":
        # Rung 4 is the governor giving up: Caution, top of sort, the user's call.
        if e.extra.get("rung") == 4:
            return AgentStatus.RESUME_FAILED, "resume failed - needs you"
        if e.extra.get("ok") is False:
            # A rung that did not bring the prompt back (no `node` in 60 s): the session is
            # still parked; the next tick tries the next rung. Never paint it as working.
            return status, f"resume attempt {e.extra.get('rung', '?')} did not start"
        return AgentStatus.WORKING, "restarted after the limit reset"
    if name == "handoff":
        # The dying/parked row is left exactly as it was; the new agent gets its own row under
        # the same tracker id -- this event names the OLD session, so it must
        # not overwrite what that row already says.
        return status, action
    return status, _clip(e.message) or action


# --------------------------------------------------------------------------- the fold


def _epoch(ts: Optional[str]) -> float:
    """Seconds since 1970, or 0.0 when the stamp is missing or unreadable. Never raises."""
    t = parse_ts(ts)
    if t is None:
        return 0.0
    try:
        return t.timestamp()
    except (ValueError, OverflowError, OSError):  # pragma: no cover - defensive
        return 0.0


def _presence_age(row: AgentState, now: datetime, statusline_mtimes: dict[str, float]) -> Optional[float]:
    """How long ago this row was last seen alive, counting a fresh statusline capture as presence
    the same way a hook event would. Whichever happened more recently -- the last event or the last statusline
    write -- wins; `statusline_mtimes` is a plain `{session_id: epoch_seconds}` mapping the caller
    builds from the filesystem, so this stays a pure function (no files, no clock of its own)."""
    event_age = row.age_seconds(now)
    mtime = statusline_mtimes.get(row.session_id) if row.session_id else None
    if mtime is None:
        return event_age
    try:
        statusline_age = max(0.0, now.timestamp() - mtime)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return event_age
    if event_age is None:
        return statusline_age
    return min(event_age, statusline_age)          # the newer of the two is the smaller age


def _is_present(
    row: AgentState,
    w: Optional[TmuxWindow],
    now: datetime,
    statusline_mtimes: dict[str, float],
) -> bool:
    """Is this agent still around? A live window always counts. Otherwise the tolerance depends
    on whether the row ever had a tmux pane at all:

    - it recorded a pane but no live window has that pane id any more -- it WAS in tmux and the
      window just is not there, so a short tolerance (`DEAD_PANE_SECONDS`) catches a killed window
      quickly instead of saying "working" for up to half an hour;
    - it never had a pane (`desktop`: the Claude Desktop app, or a plain terminal) -- the longer
      `PRESENCE_SECONDS` tolerance applies, extended by a fresh statusline capture (`_presence_age`)
      because that is the only sign of life a desktop session ever gives between hook events.
    """
    if w is not None:
        return True
    if row.tmux_pane:
        age = row.age_seconds(now)
        return age is not None and age <= DEAD_PANE_SECONDS
    age = _presence_age(row, now, statusline_mtimes)
    return age is not None and age <= PRESENCE_SECONDS


def _apply_quiet(
    row: AgentState,
    w: Optional[TmuxWindow],
    now: datetime,
    statusline_mtimes: dict[str, float],
) -> None:
    """Demote a row that has gone quiet (QUIET_AFTER_SECONDS with no fresh sign of life, the same
    presence measure `_is_present` already uses so a live statusline write still counts as
    activity even when hook events have not arrived):

    - `working`/`running` -> `quiet` -- it is not doing anything the deck can see any more,
      whatever its last event claimed (the SessionStart-forever bug); does not touch
      `winding_down`;
    - `waiting-input` with no tmux window at all -> `quiet` -- a finished desktop run nobody can
      jump to or answer stops shouting for the user once it has sat this long. A `waiting-input` row
      that DOES have a window is left exactly as it was: that one is real and stays "needs you"
      with no expiry.
    """
    age = _presence_age(row, now, statusline_mtimes)
    if age is None or age <= QUIET_AFTER_SECONDS:
        return
    if row.status in (AgentStatus.WORKING, AgentStatus.RUNNING):
        row.status = AgentStatus.QUIET
    elif row.status == AgentStatus.WAITING_INPUT and w is None:
        row.status = AgentStatus.QUIET


def _root_cwd_project(cwd: Optional[str], projects_root: str, session_id: Optional[str] = None,
                       claude_home=None) -> Optional[str]:
    """The project label for a session whose cwd is `projects_root` ITSELF, not a subfolder of it
: `derive_project` returns `None` for this case
    (it only matches a subpath), which is the root cause of the Remote Control dispatch
    conversation showing as no-project in the pit. Every such row gets `workspace`; refined to
    `dispatch` when the transcript's title is the Remote Control fingerprint
    (`transcripts.is_dispatch_title` -- the "work the plate" orchestrator, which also has a root
    cwd, keeps its own descriptive title and stays `workspace`). `session_id=None` skips the transcript read -- there is no session id to
    find a transcript file with yet -- and returns `workspace` outright."""
    if not cwd or not projects_root or norm_path(cwd) != norm_path(projects_root):
        return None
    if session_id:
        title = transcripts_mod.read_transcript(session_id, cwd, claude_home).title
        if transcripts_mod.is_dispatch_title(title):
            return "dispatch"
    return "workspace"


def _fold_one_session(key: str, events: list[Event], projects_root: str) -> AgentState:
    ordered = sorted(events, key=lambda e: (_epoch(e.ts), e.ts or ""))
    row = AgentState(session_id=key, provider="claude")
    status: AgentStatus = AgentStatus.UNKNOWN
    action = ""
    for e in ordered:
        if e.ts:
            row.last_event_ts = e.ts
        if e.cwd:
            row.cwd = e.cwd
        if e.project:
            row.project = e.project
        if e.tmux_pane:
            row.tmux_pane = e.tmux_pane
        if e.job_id:
            row.job_id = e.job_id
        if e.extra.get("tracker_id"):
            row.tracker_id = str(e.extra["tracker_id"])
        if e.extra.get("permission_mode"):
            # The hook records this on every event; the newest one wins, same as cwd/project.
            row.mode = str(e.extra["permission_mode"])
        source = (e.source or "claude").lower()
        if source == "codex":
            row.provider = "codex"
            status, action = _fold_codex(e, status, action)
        elif source == "pantheon":
            status, action = _fold_pantheon(e, status, action)
        else:
            status, action = _fold_claude(e, status, action)
    row.status = status
    row.last_action = action
    if not row.project:
        row.project = derive_project(row.cwd, projects_root) or _root_cwd_project(
            row.cwd, projects_root, row.session_id)
    return row


def _unknown_row(w: TmuxWindow, pantheon_session: str, projects_root: str) -> AgentState:
    """A live agent pane no event has claimed: real, running, but pre-hook."""
    return AgentState(
        session_id=f"pane:{w.pane_id or w.index}",
        provider="codex" if "codex" in (w.command or "").lower() else "claude",
        status=AgentStatus.UNKNOWN,
        project=derive_project(w.path, projects_root) or _root_cwd_project(w.path, projects_root)
        or (w.name or None),
        cwd=w.path,
        last_action="no events yet",
        window_index=w.index,
        window_name=w.name,
        tmux_pane=w.pane_id or None,
        tmux_session=w.session or None,
        in_pantheon=w.session == pantheon_session,
    )


def _sort_key(row: AgentState) -> tuple:
    return (SORT_RANK.get(row.status, 5), -_epoch(row.last_event_ts), row.project or "", row.session_id)


def fold(
    events: list[Event],
    windows: list[TmuxWindow],
    now: datetime,
    pantheon_session: str = "pantheon",
    projects_root: str = "",
    statusline_mtimes: Optional[dict[str, float]] = None,
    reserved_panes: Iterable[str] = (),
) -> list[AgentState]:
    """Events + live windows -> the rows the supervisor draws, already sorted.

    `reserved_panes`: pane ids no session may be matched to by folder (the Assistant's pane).

    `now` is passed in so ages and the hide-a-finished-row rule are testable.
    `statusline_mtimes` is an optional `{session_id: epoch_seconds}` map -- when the file's mtime
    is newer than a "desktop" row's last event, that counts as presence too (`_presence_age`);
    the caller reads the filesystem (`hud/sources.statusline_mtimes`), this function never does.
    """
    windows = list(windows or [])
    statusline_mtimes = statusline_mtimes or {}
    visible: list[AgentState] = []
    claimed: set[int] = set()

    folded = [_fold_one_session(key, group, projects_root)
              for key, group in by_session(events or []).items() if key and key != "?"]
    # Pane ids first, for every session, so the folder fallback below (non-Claude only) can never
    # hand a window to one session that another session's own pane id already names.
    matched: dict[int, Optional[TmuxWindow]] = {}
    taken: set[str] = set(reserved_panes or ())
    for row in folded:
        w = match_window(None, row.tmux_pane, windows, pantheon_session) if row.tmux_pane else None
        matched[id(row)] = w
        if w is not None and w.pane_id:
            taken.add(w.pane_id)
    for row in folded:
        if matched[id(row)] is None:
            matched[id(row)] = match_window(row.cwd, None, windows, pantheon_session,
                                            provider=row.provider, taken=taken)

    for row in folded:
        w = matched[id(row)]
        if w is not None:
            row.window_index = w.index
            row.window_name = w.name
            row.tmux_pane = row.tmux_pane or w.pane_id or None
            row.tmux_session = w.session or None
            row.in_pantheon = w.session == pantheon_session

        _apply_quiet(row, w, now, statusline_mtimes)

        if not _is_present(row, w, now, statusline_mtimes):
            # DONE/FAILED are terminal: a finished job is SUPPOSED to have no sign of life, and
            # relabeling it `gone` erases the one fact the user needs. Only a row that never reported an ending gets demoted.
            if row.status not in (AgentStatus.GONE, AgentStatus.DONE, AgentStatus.FAILED):
                row.status = AgentStatus.GONE
                row.last_action = row.last_action or "no sign of it"
        # A finished agent lingers so the user can see it ended, then leaves the screen for good --
        # done/failed rows age off on the same clock as gone ones.
        age = row.age_seconds(now)
        if row.status in (AgentStatus.GONE, AgentStatus.DONE, AgentStatus.FAILED) and (
                age is None or age > HIDE_GONE_AFTER_SECONDS):
            continue

        if w is not None:
            claimed.add(id(w))
        visible.append(row)

    # tmux reuses pane ids: a session whose window was killed can still "match" the new window
    # that took its pane id.
    # Only the most recent session keeps a window; the others lose it and age out as gone.
    by_pane: dict[tuple, list[AgentState]] = {}
    for row in visible:
        if row.window_index is not None:
            by_pane.setdefault((row.tmux_session, row.window_index), []).append(row)
    for group in by_pane.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: -_epoch(r.last_event_ts))
        for stale in group[1:]:
            stale.window_index = None
            stale.window_name = None
            stale.tmux_session = None
            stale.in_pantheon = False
            if not _is_present(stale, None, now, statusline_mtimes) and stale.status not in (
                    AgentStatus.DONE, AgentStatus.FAILED):
                stale.status = AgentStatus.GONE
                stale.last_action = "no sign of it"
    visible = [
        r for r in visible
        if not (r.status == AgentStatus.GONE and (r.age_seconds(now) is None or r.age_seconds(now) > HIDE_GONE_AFTER_SECONDS))
    ]

    # Live agent panes nobody claimed still deserve a row -- the user can see them, so must the deck.
    seen_keys = {r.session_id for r in visible}
    for w in windows:
        if id(w) in claimed or not _is_agent_window(w):
            continue
        row = _unknown_row(w, pantheon_session, projects_root)
        if row.session_id in seen_keys:      # the same pane under two grouped sessions
            continue
        seen_keys.add(row.session_id)
        visible.append(row)

    visible.sort(key=_sort_key)
    return visible


# --------------------------------------------------------------------------- startup dialogs


# A Claude Code window stuck on a startup question (folder trust, a new MCP server; see
# `pantheon/dialogs.py`) fires no hook, so the only way to spot it is to read its screen. Reading
# costs one `capture-pane` per window, so only rows that could be stuck are read: a live window,
# Claude, and no hook event in the last `DIALOG_FRESH_SECONDS` (a session that just sent one is
# past its startup questions, whatever it is doing now).
DIALOG_FRESH_SECONDS = 20


def dialog_candidates(rows: Iterable[AgentState], now: datetime) -> list[AgentState]:
    out = []
    for row in rows:
        if row.window_index is None or not row.tmux_session or row.provider != "claude":
            continue
        age = row.age_seconds(now)
        if age is not None and age < DIALOG_FRESH_SECONDS:
            continue
        out.append(row)
    return out


def apply_dialogs(rows: list[AgentState], reasons: dict[str, str]) -> list[AgentState]:
    """Mark each row in `reasons` (session_id -> plain words, e.g. `asking whether to trust
    loom-os`) as needing the user: the Caution tier every "needs you" surface already counts (the
    NEEDS YOU card, the command bar's `need you` pill, notify's rules), with the reason as its
    last action. Re-sorted so it rises to the top like a permission prompt."""
    if not reasons:
        return rows
    for row in rows:
        reason = reasons.get(row.session_id)
        if reason:
            row.status = AgentStatus.BLOCKED_PERMISSION
            row.last_action = reason[:MESSAGE_CHARS]
    return sorted(rows, key=_sort_key)


# --------------------------------------------------------------------------- footer text


# A row in one of these states has ended: it is never counted as running anywhere (the sidebar's
# `N running`, a project heading, THE PIT's subtitle) and the sidebar lists it with that project's
# finished conversations.
ENDED = frozenset({AgentStatus.GONE, AgentStatus.DONE, AgentStatus.FAILED})


def is_running(row: AgentState) -> bool:
    return row.status not in ENDED


def running_count(rows: Iterable[AgentState]) -> int:
    return sum(1 for r in rows if is_running(r))


def counts(rows: Iterable[AgentState]) -> tuple[int, int]:
    """(working, needs a human) -- the two numbers in the header line."""
    rows = list(rows)
    working = sum(
        1 for r in rows if r.status in (AgentStatus.WORKING, AgentStatus.RUNNING, AgentStatus.WINDING_DOWN)
    )
    return working, sum(1 for r in rows if r.needs_human)


def footer_status(parse_errors: int, live_tmux: bool) -> str:
    """The right-hand end of the footer. The parse-error count is only shown when there are some."""
    bits = []
    if parse_errors > 0:
        bits.append(f"parse errors: {parse_errors}")
    bits.append("(live: tmux)" if live_tmux else "(no tmux)")
    return "  ".join(bits)
