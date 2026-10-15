"""Restart everything and bring every session back.

the user's brief (2026-09-29): "Restart needs to restart all of pantheon and claude code instead of
that bullshit, and reopen the existing threads." One full restart picks up new Pantheon code AND
a newly installed Claude Code, and every conversation comes back where it was.

The order, and who does each part:

  1. snapshot + wait (this module's `prepare`, run by `bin/pantheon --restart --all`): every
     Claude Code session in the Pantheon tmux session is written to `state/restart_snapshot.json`
     -- its conversation id, folder and window name -- and the same for Codex. Nothing is ended
     until every Claude session is between turns (idle, or waiting on the user); the waiter says
     which ones it is still waiting for and can be cancelled (Ctrl+C in a shell, Esc on the deck's
     wait screen, `pantheon/restart.py`). Fail closed: a Claude window with no known conversation
     id, or a snapshot that will not write, stops the restart before anything is ended.
  2. the detached part (`run`, started with `setsid nohup` so it outlives the tmux server it
     ends): running headless Codex jobs are moved into a holding tmux session so they keep
     running; the base session and every per-client grouped session are ended (what
     `--restart --all` always did); Pantheon is started again on the SSH side
     (`bin/pantheon --no-attach` over `ssh localhost`, CLAUDE.md: the server must live in Windows
     session 0); then `reopen` runs there.
  3. reopen: each saved Claude session opens in its own window, named as before, with
     `claude --resume <id>` in its saved folder -- through the same `start_session` +
     `ClaudeProvider.launch` path the deck's "resume a session" uses, so Remote Control comes on
     exactly as it does today (the `claude` wrapper adds it). The Assistant is NOT reopened here:
     the deck brings it back on start through its own path (`pantheon/assistant.py`, the id in
     `state/assistant.json`). Parked Codex jobs move back into the new session. What happened is
     written to `state/restart_result.json`; the deck reads it and names every session that did
     not come back, with its id.

Codex (checked 2026-09-29 against providers/codex.py):
Pantheon's Codex sessions are headless `codex exec` jobs (one prompt, run to the end) or
interactive Codex in a native Windows console outside tmux (interactive Codex does not run under
MSYS2 tmux, OpenAI issue #6994). Neither has a between-turns state a restart could resume: a
headless job is mid-run until it ends, and the native console is not in the tmux server at all,
so a restart never touches it. So no Codex session is resumed; running headless jobs are kept
running (parked and moved back) and the deck names them.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import assistant as assistant_mod
from . import config as config_mod
from . import eventcache as eventcache_mod
from . import tmuxctl
from .hud import sources as hud_sources
from .models import AgentState, AgentStatus
from .supervisor import state as state_mod

log = logging.getLogger("pantheon.fullrestart")

SNAPSHOT_FILE = "restart_snapshot.json"
RESULT_FILE = "restart_result.json"
HOLD_SUFFIX = "_restart_hold"      # never `<session>-...`: that pattern is what gets ended
POLL_SECONDS = 2.0

# Between turns: safe to end and resume. `waiting-input` / `blocked-permission` / `idle` are
# Claude sitting at its prompt or on a question for the user (spec: "idle or waiting on the user").
# `quiet` = no sign of life for 10+ minutes (supervisor/state.py QUIET_AFTER_SECONDS) -- not a
# turn the deck can see in progress, and treating it as busy would block the restart forever
# on one stale row.
SETTLED = {AgentStatus.WAITING_INPUT, AgentStatus.BLOCKED_PERMISSION, AgentStatus.IDLE,
           AgentStatus.QUIET, AgentStatus.PARKED, AgentStatus.RESUME_FAILED}
# A session that has only reported its start (a fresh or just-resumed window at its prompt) is
# folded as `working, session started` until it takes a turn; it is not mid-turn.
JUST_STARTED = "session started"
# Codex headless jobs still running: parked across the restart, not waited for.
CODEX_RUNNING = {AgentStatus.RUNNING, AgentStatus.QUEUED, AgentStatus.WORKING, AgentStatus.UNKNOWN}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / SNAPSHOT_FILE


def result_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / RESULT_FILE


def with_session_override(cfg: config_mod.Config) -> config_mod.Config:
    """`PANTHEON_SESSION` beats pantheon.toml's session name, the same override bin/pantheon and
    its launcher test lane use everywhere else."""
    name = os.environ.get("PANTHEON_SESSION", "").strip()
    return dataclasses.replace(cfg, tmux_session=name) if name else cfg


# --------------------------------------------------------------------------- reading the sessions


def current_rows(cfg: config_mod.Config, windows: Optional[list] = None,
                 events: Optional[list] = None, now: Optional[datetime] = None) -> list[AgentState]:
    """The supervisor's own rows, folded from the hook events it already records plus the live
    tmux windows -- the same call the deck's table makes (`SupervisorPane.refresh_rows`)."""
    if events is None:
        events, _errors = eventcache_mod.read_events_cached(cfg.events_file)
    if windows is None:
        windows = tmuxctl.list_windows(None, tmux=cfg.tools.tmux)
    return state_mod.fold(
        events, windows, now or datetime.now(timezone.utc), cfg.tmux_session, cfg.projects_root,
        statusline_mtimes=hud_sources.statusline_mtimes(cfg.statusline_dir),
        reserved_panes=assistant_mod.reserved_panes(cfg),
    )


def _in_session(row: AgentState, session: str) -> bool:
    """A row whose window a full restart ends: in the base session or a grouped client session
    (`list_windows` already collapses those onto the base name)."""
    if row.window_index is None:
        return False
    name = row.tmux_session or ""
    return name == session or name.startswith(session + "-")


def claude_rows(rows: Iterable[AgentState], session: str) -> list[AgentState]:
    return [r for r in rows if r.provider == "claude" and _in_session(r, session)
            and r.status not in (AgentStatus.GONE, AgentStatus.DONE, AgentStatus.FAILED)]


def codex_rows(rows: Iterable[AgentState], session: str) -> list[AgentState]:
    return [r for r in rows if r.provider == "codex" and _in_session(r, session)]


def has_conversation_id(row: AgentState) -> bool:
    """`pane:%7` is the supervisor's name for a live agent window no hook has spoken for yet --
    there is no conversation id to resume."""
    sid = row.session_id or ""
    return bool(sid) and not sid.startswith("pane:") and sid != "?"


def is_busy(row: AgentState) -> bool:
    """Mid-turn: ending it now would cut a turn off half-way."""
    if row.status in SETTLED:
        return False
    if row.status == AgentStatus.WORKING and row.last_action == JUST_STARTED:
        return False
    return True


def label(row: AgentState) -> str:
    """`window 4 garden_notes (working)` -- what the waiter and every failure message print."""
    name = row.window_name or row.project or "?"
    where = f"window {row.window_index} " if row.window_index is not None else ""
    return f"{where}{name} ({row.status.label})"


def waiting_on(rows: Iterable[AgentState], session: str) -> list[AgentState]:
    """The Claude sessions a full restart still has to wait for."""
    return [r for r in claude_rows(rows, session) if is_busy(r)]


# --------------------------------------------------------------------------- the snapshot


def build_snapshot(cfg: config_mod.Config, rows: Iterable[AgentState],
                   assistant_state: Optional[dict] = None) -> tuple[dict, list[str]]:
    """(snapshot, problems). A problem is a reason not to end anything: a Claude window whose
    conversation id is not known (spec failure behaviour: stop and say which)."""
    rows = list(rows)
    session = cfg.tmux_session
    state = assistant_state if assistant_state is not None else (
        assistant_mod.read_state(cfg) if cfg.assistant_settings().enabled else {})
    problems: list[str] = []
    claude: list[dict] = []
    for row in claude_rows(rows, session):
        if not has_conversation_id(row):
            problems.append(f"{label(row)} has no known conversation id")
            continue
        if not row.cwd:
            problems.append(f"{label(row)} has no known folder")
            continue
        claude.append({
            "session_id": row.session_id,
            "folder": row.cwd,
            "window_name": row.window_name or Path(row.cwd).name,
            "window_index": row.window_index,
            "role": "assistant" if state and assistant_mod.owns(row, state) else "session",
        })
    codex: list[dict] = []
    for row in codex_rows(rows, session):
        running = row.status in CODEX_RUNNING
        codex.append({
            "session_id": row.session_id,
            "job_id": row.job_id,
            "folder": row.cwd,
            "window_name": row.window_name,
            "window_index": row.window_index,
            "tmux_pane": row.tmux_pane,
            "status": row.status.value,
            # No Codex session can be resumed from here (module docstring): a running job is
            # kept running instead, a finished one has nothing left to keep.
            "action": "keep-running" if running else "finished",
        })
    snapshot = {
        "written_at": _now_iso(),
        "tmux_session": session,
        "claude": claude,
        "codex": codex,
        "assistant": {"session_id": state.get("session_id"), "folder": state.get("folder")}
        if state else None,
    }
    return snapshot, problems


def write_snapshot(cfg: config_mod.Config, snapshot: dict) -> Path:
    """Atomic write; raises OSError when it cannot (the caller stops before ending anything)."""
    path = snapshot_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_snapshot(cfg: config_mod.Config) -> dict:
    try:
        data = json.loads(snapshot_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def wait_until_settled(cfg: config_mod.Config, rows_source: Callable[[], list[AgentState]],
                       say: Callable[[str], None] = print,
                       sleep: Callable[[float], None] = time.sleep,
                       timeout: Optional[float] = None,
                       clock: Callable[[], float] = time.monotonic) -> list[AgentState]:
    """Poll until no Claude session is mid-turn; return the final rows. Says who it is waiting
    for whenever that list changes. `KeyboardInterrupt` (Ctrl+C) propagates -- the caller turns
    it into "cancelled; nothing was ended". `timeout` None = wait as long as it takes."""
    started = clock()
    last: Optional[tuple] = None
    while True:
        rows = rows_source()
        busy = waiting_on(rows, cfg.tmux_session)
        if not busy:
            return rows
        names = tuple(label(r) for r in busy)
        if names != last:
            say("waiting for these sessions to finish their turn (Ctrl+C cancels, nothing is ended): "
                + "; ".join(names))
            last = names
        if timeout is not None and clock() - started >= timeout:
            raise TimeoutError("; ".join(names))
        sleep(POLL_SECONDS)


def prepare(cfg: config_mod.Config, wait: bool = True,
            rows_source: Optional[Callable[[], list[AgentState]]] = None,
            say: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> int:
    """Step 1: wait, then write the snapshot. 0 = ready to end everything; anything else = stop,
    nothing was ended. `wait=False` (the deck already waited on its own screen): a session that
    started a turn since then stops the restart instead of being waited on invisibly."""
    source = rows_source or (lambda: current_rows(cfg))
    try:
        # The Assistant's saved id follows a `/clear` inside it; bring the record up to date
        # before it is copied into the snapshot.
        if cfg.assistant_settings().enabled:
            assistant_mod.learn(cfg)
    except Exception:
        log.exception("could not refresh the Assistant's saved id")
    rows = source()
    _snap, problems = build_snapshot(cfg, rows)
    if problems:
        say("not restarting, nothing was ended: " + "; ".join(problems))
        return 2
    if wait:
        try:
            rows = wait_until_settled(cfg, source, say=say, sleep=sleep)
        except KeyboardInterrupt:
            say("restart cancelled; nothing was ended")
            return 130
    else:
        busy = waiting_on(rows, cfg.tmux_session)
        if busy:
            say("not restarting, nothing was ended: these sessions started a turn: "
                + "; ".join(label(r) for r in busy))
            return 3
    snapshot, problems = build_snapshot(cfg, rows)
    if problems:
        say("not restarting, nothing was ended: " + "; ".join(problems))
        return 2
    try:
        path = write_snapshot(cfg, snapshot)
    except OSError as exc:
        say(f"not restarting, nothing was ended: could not write the snapshot ({exc})")
        return 2
    sessions = [e for e in snapshot["claude"] if e["role"] == "session"]
    say(f"snapshot written to {path}: {len(sessions)} Claude session(s) to reopen"
        + (", the Assistant" if any(e["role"] == "assistant" for e in snapshot["claude"]) else "")
        + (f", {sum(1 for c in snapshot['codex'] if c['action'] == 'keep-running')} Codex job(s) kept running"
           if snapshot["codex"] else ""))
    return 0


# --------------------------------------------------------------------------- the detached part


def hold_session(cfg: config_mod.Config) -> str:
    return f"{cfg.tmux_session}{HOLD_SUFFIX}"


def _ok(cp) -> bool:
    return getattr(cp, "returncode", 1) == 0


def park_codex(cfg: config_mod.Config, snapshot: dict, run: Optional[Callable] = None) -> tuple[list, list]:
    """Move every running headless Codex job's window into a holding tmux session, so ending the
    Pantheon session does not end the job. (moved, failed) -- window names."""
    run = run or tmuxctl.run
    tmux = cfg.tools.tmux
    keep = [c for c in snapshot.get("codex") or [] if c.get("action") == "keep-running"
            and (c.get("tmux_pane") or c.get("window_index") is not None)]
    if not keep:
        return [], []
    hold = hold_session(cfg)
    if not _ok(run("has-session", "-t", f"={hold}", tmux=tmux)):
        # A placeholder window: a tmux session cannot exist with no window in it.
        if not _ok(run("new-session", "-d", "-s", hold, "-n", "hold", "sleep 86400", tmux=tmux)):
            return [], [c.get("window_name") or "?" for c in keep]
    moved, failed = [], []
    for c in keep:
        source = c.get("tmux_pane") or f"{cfg.tmux_session}:{c['window_index']}"
        name = c.get("window_name") or c.get("job_id") or "?"
        (moved if _ok(run("move-window", "-d", "-s", source, "-t", f"{hold}:", tmux=tmux)) else failed).append(name)
    return moved, failed


def unpark_codex(cfg: config_mod.Config, run: Optional[Callable] = None) -> tuple[list, list]:
    """Move parked Codex windows back into the (new) Pantheon session; end the holding session
    once only its placeholder is left. (moved back, still parked) -- window names."""
    run = run or tmuxctl.run
    tmux = cfg.tools.tmux
    hold = hold_session(cfg)
    cp = run("list-windows", "-t", f"={hold}", "-F", "#{window_index}|#{window_name}", tmux=tmux)
    if not _ok(cp):
        return [], []
    back, stuck = [], []
    for line in (cp.stdout or "").splitlines():
        index, _, name = line.partition("|")
        if name == "hold":
            continue
        ok = _ok(run("move-window", "-d", "-s", f"{hold}:{index}", "-t", f"{cfg.tmux_session}:", tmux=tmux))
        (back if ok else stuck).append(name)
    if not stuck:
        run("kill-session", "-t", f"={hold}", tmux=tmux)
    return back, stuck


def end_sessions(cfg: config_mod.Config, run: Optional[Callable] = None) -> list[str]:
    """End the base session and every per-client grouped session (`<session>-<pid>`), agent
    windows included -- exactly what `--restart --all` always ended. The holding session
    (`<session>_restart_hold`) does not match and survives."""
    run = run or tmuxctl.run
    cp = run("list-sessions", "-F", "#{session_name}", tmux=cfg.tools.tmux)
    ended = []
    for name in (cp.stdout or "").splitlines() if _ok(cp) else []:
        name = name.strip()
        if name == cfg.tmux_session or name.startswith(cfg.tmux_session + "-"):
            run("kill-session", "-t", f"={name}", tmux=cfg.tools.tmux)
            ended.append(name)
    return ended


def use_ssh() -> bool:
    """The SSH route is the default (CLAUDE.md: the tmux server must come back in Windows session
    0). A throwaway tmux server (the launcher test lane sets TMUX_TMPDIR) is not reachable over
    ssh, which would find the LIVE server instead; PANTHEON_RESTART_LOCAL=1 forces local too."""
    return not (os.environ.get("TMUX_TMPDIR") or os.environ.get("PANTHEON_RESTART_LOCAL") == "1")


def _env_prefix(cfg: config_mod.Config) -> str:
    env = f"PANTHEON_SESSION={shlex.quote(cfg.tmux_session)}"
    state_dir = os.environ.get("PANTHEON_STATE_DIR")
    if state_dir:
        env += f" PANTHEON_STATE_DIR={shlex.quote(state_dir)}"
    return env


def ssh_start_command(cfg: config_mod.Config, root: Optional[Path] = None,
                      ssh_bin: Optional[str] = None) -> list[str]:
    """The argv that starts Pantheon on the SSH side and then reopens the saved sessions there:
    the same `ssh -o BatchMode=yes localhost '...bin/pantheon ...'` route CLAUDE.md documents for
    `--restart`, with `--no-attach` (nobody is at this terminal)."""
    from .dispatch import reach as reach_mod

    root = Path(root or config_mod.PROJECT_ROOT)
    launcher = shlex.quote((root / "bin" / "pantheon").as_posix())
    py = shlex.quote((root / ".venv" / "bin" / "python").as_posix())
    env = _env_prefix(cfg)
    remote = (f"export PATH=/usr/bin:$PATH MSYSTEM=MSYS; {env} {launcher} --no-attach && "
              f"cd {shlex.quote(root.as_posix())} && {env} {py} -m pantheon.fullrestart reopen")
    return [reach_mod.ssh_path(ssh_bin), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "localhost", remote]


def resume_options(entry: dict) -> dict:
    """What the deck's own "resume a session" path takes (`start_session` -> `ClaudeProvider
    .launch`): the id becomes `--resume <id>`, the window gets its old name back."""
    return {"resume": entry["session_id"], "window_name": entry.get("window_name") or ""}


def resume_command(cfg: config_mod.Config, entry: dict) -> str:
    """The Claude command typed into the reopened window (after `cd "<folder>"`); the launch adds
    the exit recorder after it (`providers.claude.exit_suffix`)."""
    from .providers import claude as claude_provider

    return claude_provider.claude_command(cfg.tools.claude, resume_options(entry))


def reopen(cfg: config_mod.Config, snapshot: dict, provider=None,
           start: Optional[Callable] = None) -> tuple[list, list]:
    """Reopen each saved Claude session (not the Assistant: the deck brings that one back itself)
    in its own window. (resumed, failed) -- snapshot entries, a failed one with its `message`."""
    from .dispatch import projects as projects_mod
    from .providers.claude import ClaudeProvider

    provider = provider or ClaudeProvider(cfg)
    start = start or projects_mod.start_session
    resumed, failed = [], []
    for entry in snapshot.get("claude") or []:
        if entry.get("role") == "assistant":
            continue
        try:
            result = start(entry["folder"], cfg, provider, True, "", resume_options(entry))
        except Exception as exc:
            log.exception("reopening %s failed", entry.get("session_id"))
            failed.append({**entry, "message": f"could not start: {exc}"})
            continue
        if getattr(result, "ok", False):
            resumed.append({**entry, "window_index": result.window_index})
        else:
            failed.append({**entry, "message": getattr(result, "message", "") or "did not start"})
    return resumed, failed


def write_result(cfg: config_mod.Config, result: dict) -> None:
    try:
        path = result_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.exception("could not write %s", result_path(cfg))


def read_result(cfg: config_mod.Config) -> dict:
    try:
        data = json.loads(result_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def result_message(result: dict) -> tuple[str, bool]:
    """(one status line for the deck, whether it is a warning). Every session that did not come
    back is named with its id (spec: listed in the deck, never silently dropped)."""
    if not result:
        return "", False
    if result.get("error"):
        where = "state/health/watch.log" if result.get("after_freeze") else "state/restart.log"
        return f"restart: {result['error']} - details in {where}", True
    parts = [f"restart: {len(result.get('resumed') or [])} session(s) back on their conversations"]
    warn = False
    failed = result.get("failed") or []
    if failed:
        warn = True
        parts.append("did NOT reopen: " + "; ".join(
            f"{f.get('window_name') or '?'} ({f.get('session_id')})" for f in failed))
    kept = result.get("codex_kept") or []
    if kept:
        parts.append("Codex jobs kept running, not resumed: " + ", ".join(kept))
    lost = result.get("codex_lost") or []
    if lost:
        warn = True
        parts.append("Codex jobs lost in the freeze: " + ", ".join(lost))
    stuck = result.get("codex_stuck") or []
    if stuck:
        warn = True
        parts.append(f"Codex jobs still in tmux session {result.get('hold_session')}: " + ", ".join(stuck))
    return " | ".join(parts), warn


def reopen_step(cfg: config_mod.Config, run: Optional[Callable] = None, provider=None,
                start: Optional[Callable] = None, sleep: Callable[[float], None] = time.sleep,
                wait_seconds: float = 30.0, snapshot: Optional[dict] = None) -> dict:
    """Runs on the SSH side once the new Pantheon session is up: parked Codex back, every saved
    Claude session reopened, the outcome written for the deck. `snapshot` None = the saved
    snapshot file; the freeze restart passes the watcher's last healthy snapshot instead."""
    run = run or tmuxctl.run
    snapshot = read_snapshot(cfg) if snapshot is None else snapshot
    waited = 0.0
    while not _ok(run("has-session", "-t", f"={cfg.tmux_session}", tmux=cfg.tools.tmux)):
        if waited >= wait_seconds:
            result = {"finished_at": _now_iso(), "error": "Pantheon did not come back, so no session was reopened",
                      "resumed": [], "failed": [e for e in snapshot.get("claude") or [] if e.get("role") != "assistant"]}
            write_result(cfg, result)
            return result
        sleep(1.0)
        waited += 1.0
    back, stuck = unpark_codex(cfg, run=run)
    resumed, failed = reopen(cfg, snapshot, provider=provider, start=start)
    result = {"finished_at": _now_iso(), "resumed": resumed, "failed": failed,
              "codex_kept": back, "codex_stuck": stuck, "hold_session": hold_session(cfg)}
    write_result(cfg, result)
    for f in failed:
        log.warning("did not reopen %s in %s: %s", f.get("session_id"), f.get("folder"), f.get("message"))
    return result


def run_restart(cfg: config_mod.Config, run: Optional[Callable] = None,
                spawn: Optional[Callable] = None, root: Optional[Path] = None) -> int:
    """The detached process: park Codex, end the sessions, start Pantheon on the SSH side, reopen.
    Fails closed while nothing has been ended yet."""
    run = run or tmuxctl.run
    spawn = spawn or (lambda argv: subprocess.run(argv, stdin=subprocess.DEVNULL, timeout=600).returncode)
    snapshot = read_snapshot(cfg)
    if not snapshot:
        print("pantheon: no restart snapshot; nothing was ended", flush=True)
        return 2
    moved, failed = park_codex(cfg, snapshot, run=run)
    if failed:
        # Nothing ended yet: put back what moved and stop.
        unpark_codex(cfg, run=run)
        print("pantheon: could not keep these Codex jobs running, so nothing was ended: "
              + ", ".join(failed), flush=True)
        write_result(cfg, {"finished_at": _now_iso(), "error": "a Codex job could not be kept running; "
                           "nothing was ended", "resumed": [], "failed": []})
        return 2
    ended = end_sessions(cfg, run=run)
    print(f"pantheon: ended {', '.join(ended) or 'no session'}; parked Codex: {', '.join(moved) or 'none'}",
          flush=True)
    root = Path(root or config_mod.PROJECT_ROOT)
    if use_ssh():
        code = spawn(ssh_start_command(cfg, root))
        if code != 0:
            write_result(cfg, {"finished_at": _now_iso(),
                               "error": f"starting Pantheon over ssh failed (exit {code}); run pantheon",
                               "resumed": [], "failed": []})
        return code
    env = dict(os.environ, PANTHEON_SESSION=cfg.tmux_session)
    code = subprocess.run([(root / "bin" / "pantheon").as_posix(), "--no-attach"], env=env,
                          stdin=subprocess.DEVNULL).returncode
    if code != 0:
        write_result(cfg, {"finished_at": _now_iso(), "error": f"pantheon --no-attach failed (exit {code})",
                           "resumed": [], "failed": []})
        return code
    reopen_step(cfg, run=run)
    return 0


def last_health_snapshot_path(cfg: config_mod.Config) -> Path:
    """The snapshot the freeze watcher saves while tmux is healthy (pantheon/health/watch.py)."""
    from .health import sample as health_sample

    return health_sample.health_dir(cfg) / "last_snapshot.json"


def run_after_freeze(cfg: config_mod.Config, run: Optional[Callable] = None,
                     spawn: Optional[Callable] = None, root: Optional[Path] = None, provider=None,
                     start: Optional[Callable] = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """The restart after a freeze, run by the
    watcher once it has ended the frozen server. `run_restart` without `park_codex` and
    `end_sessions` (on a dead server both fail, and `run_restart` stops without restarting):
    read the watcher's last healthy snapshot, start Pantheon locally with `bin/pantheon
    --no-attach` (the watcher already runs on the SSH side), then reopen every saved session.
    Headless Codex jobs on the frozen server died with it; the result names them. Runs even when
    an agent looked mid-turn. Returns the result, also written to
    `state/restart_result.json` for the deck."""
    snapshot = {}
    try:
        data = json.loads(last_health_snapshot_path(cfg).read_text(encoding="utf-8"))
        snapshot = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log.warning("no saved snapshot to resume from after the freeze")
    expected = [e for e in snapshot.get("claude") or [] if e.get("role") != "assistant"]
    lost = [c.get("window_name") or c.get("job_id") or "?" for c in snapshot.get("codex") or []
            if c.get("action") == "keep-running"]
    root = Path(root or config_mod.PROJECT_ROOT)
    spawn = spawn or (lambda argv, env: subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                                       timeout=600).returncode)
    env = dict(os.environ, PANTHEON_SESSION=cfg.tmux_session)
    try:
        code = spawn([(root / "bin" / "pantheon").as_posix(), "--no-attach"], env)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("starting Pantheon after the freeze failed: %s", exc)
        code = -1
    if code != 0:
        result = {"finished_at": _now_iso(), "after_freeze": True, "expected": len(expected),
                  "error": f"Pantheon did not start again after the freeze (exit {code})",
                  "resumed": [], "failed": expected, "codex_lost": lost}
        write_result(cfg, result)
        return result
    result = reopen_step(cfg, run=run, provider=provider, start=start, sleep=sleep, snapshot=snapshot)
    result.update(after_freeze=True, expected=len(expected), codex_lost=lost)
    write_result(cfg, result)
    return result


# --------------------------------------------------------------------------- command line


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pantheon.fullrestart")
    sub = parser.add_subparsers(dest="step", required=True)
    p = sub.add_parser("prepare", help="wait for every Claude session to be between turns, then snapshot")
    p.add_argument("--no-wait", action="store_true",
                   help="do not wait: stop instead when a session is mid-turn")
    sub.add_parser("run", help="the detached part: end the sessions, start fresh, reopen")
    sub.add_parser("reopen", help="reopen the saved sessions in the running Pantheon session")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = with_session_override(config_mod.load())
    if args.step == "prepare":
        return prepare(cfg, wait=not args.no_wait, say=lambda m: print(f"pantheon: {m}", flush=True))
    if args.step == "run":
        return run_restart(cfg)
    if args.step == "reopen":
        result = reopen_step(cfg)
        print("pantheon: " + result_message(result)[0], flush=True)
        return 0 if not result.get("failed") and not result.get("error") else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
