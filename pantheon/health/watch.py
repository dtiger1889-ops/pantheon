"""`python -m pantheon.health.watch` -- the freeze watcher.

A process of its own, outside tmux, started by `bin/pantheon` whenever it starts or finds the
server (only when the pid in `state/health/watch.pid` is not alive). A frozen server cannot block
it, and it outlives the server, so it can bring it back. One thread only, shaped like
`pantheon/hud/collector.py`: forks from worker threads have hung under MSYS2.

Every `probe_seconds` it asks tmux `list-sessions`:
  - answered: healthy. The server's Windows pid is refreshed (`state/health/server.json`); every
    `snapshot_seconds` the snapshot of the sessions is saved to
    `state/health/last_snapshot.json`; every `sample_seconds` handles and memory are sampled
    (`sample.py`) into `state/health.json` for the deck, and the warnings and the planned restart
    are checked. The first good sample in each UTC hour also appends one line to
    `state/health/handles-hourly.jsonl` (server pid, handles, memory, each claude.exe's handles),
    so the done bar's "flat handles across a day" has a recorded series (spec step 7).
  - timed out: one miss. `freeze_after_misses` misses in a row: capture, then one more probe. An
    answer there is a stall (folder renamed `stall-*`, alert, nothing ended). Still no answer is a
    freeze: storm guard, alert, end the server (`taskkill //F //T`), restart and resume
    (`fullrestart.run_after_freeze`), alert the result.
  - could not start the process (fork failed): a low-memory signal, not a miss.
  - no server running: one log line, then the watcher leaves. Server deaths are out of scope; the
    next `bin/pantheon` that starts a server starts a new watcher.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import config as config_mod
from .. import fullrestart
from .. import tmuxctl
from ..notify import channels
from . import capture as capture_mod
from . import sample as sample_mod
from . import status as status_mod

log = logging.getLogger("pantheon.health.watch")

PID_FILE = "watch.pid"
STATE_FILE = "watch_state.json"
HEALTH_FILE = status_mod.HEALTH_FILE
LAST_SNAPSHOT_FILE = "last_snapshot.json"
ALERTS_FILE = "alerts.jsonl"
LOG_FILE = "watch.log"
HOURLY_FILE = "handles-hourly.jsonl"
HOURLY_TAIL_BYTES = 65536

ANSWERED, TIMEOUT, SPAWN_FAILED, NO_SERVER, ERROR = "answered", "timeout", "spawn-failed", "no-server", "error"

TASKKILL = "C:/Windows/System32/taskkill.exe"
KILL_TIMEOUT_SECONDS = 30
LAUNCH_TIMEOUT_SECONDS = 120
PLANNED_EVERY_SECONDS = 6 * 3600
PLANNED_BACKOFF_WINDOW_SECONDS = 3600
RESULT_WAIT_SECONDS = 900
RESULT_POLL_SECONDS = 2.0

# Empty panes. Empty = a dead pane, or a shell with no process under it; the
# deck's own windows run their python under a bash too, so the command name alone is not enough.
# Only MSYS2 shells: a program started from a native shell (pwsh, cmd) never appears in MSYS2's
# /proc as its child, so an idle-looking pwsh may be running Claude (Sol audit 2026-10-15).
EMPTY_CHECK_SECONDS = 60
EMPTY_SHELLS = frozenset({"bash", "-bash", "sh", "dash", "zsh", "fish"})
KEPT_WINDOWS = frozenset({"DECK", "QUEUE", "BUDGET", "ASSISTANT"})
EMPTY_FMT = "#{pane_id}|#{pane_pid}|#{pane_dead}|#{window_activity}|#{pane_current_command}|#{window_name}"

# Lines in state/pantheon.log that mean a process could not get memory (2026-09-24..29).
LOW_MEMORY_LINES = ("Resource temporarily unavailable",)


def health_dir(cfg) -> Path:
    return sample_mod.health_dir(cfg)


def health_path(cfg) -> Path:
    return status_mod.health_path(cfg)


def last_snapshot_path(cfg) -> Path:
    return health_dir(cfg) / LAST_SNAPSHOT_FILE


def pid_path(cfg) -> Path:
    return health_dir(cfg) / PID_FILE


def hourly_path(cfg) -> Path:
    return health_dir(cfg) / HOURLY_FILE


def last_hour_written(path: Path) -> str:
    """The UTC hour ("YYYY-MM-DDTHH") of the newest line in the hourly file, "" when none. Read
    once per watcher start, so a restarted watcher does not write a second line for the same hour."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - HOURLY_TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return ""
    for line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        try:
            ts = json.loads(line).get("ts")
        except (ValueError, AttributeError):
            continue
        if isinstance(ts, str) and len(ts) >= 13:
            return ts[:13]
    return ""


def hourly_line(at: float, server: dict, sample: dict) -> dict:
    """One line of the hourly handle log: when, the tmux server (both pids, handles, threads,
    memory), memory commit, and each claude.exe's pid, handles and memory."""
    srv = (sample or {}).get("server") or {}
    srv = srv if isinstance(srv, dict) else {}
    claude = []
    for p in (sample or {}).get("claude") or []:
        if isinstance(p, dict):
            claude.append({"pid": p.get("pid"), "handles": p.get("handles"),
                           "private_mb": p.get("private_mb")})
    return {
        "ts": _iso(at),
        "server_msys_pid": (server or {}).get("msys_pid"),
        "server_winpid": srv.get("pid") or (server or {}).get("winpid"),
        "server_handles": srv.get("handles"),
        "server_threads": srv.get("threads"),
        "server_private_mb": srv.get("private_mb"),
        "commit_used_mb": sample.get("commit_used_mb"),
        "commit_limit_mb": sample.get("commit_limit_mb"),
        "commit_percent": sample.get("commit_percent"),
        "claude_count": len(claude),
        "claude_handles_total": sum(c["handles"] for c in claude if isinstance(c.get("handles"), int)),
        "claude": claude,
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, data) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        log.exception("could not write %s", path)
        return False


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_low_memory_line(line: str) -> bool:
    return ("memory allocation of" in line and "failed" in line) or any(p in line for p in LOW_MEMORY_LINES)


def _event_time(value) -> Optional[float]:
    """An event's time as epoch seconds: a number (seconds or milliseconds), or an ISO 8601
    string; None when neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            try:
                return _event_time(float(text))
            except ValueError:
                return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp()
    return None


def _default_spawn(argv: list[str], timeout: float, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Every outside program the watcher runs: null input, output captured, capped in time."""
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, env=env, check=False)


def _default_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, OSError):
        return True
    return True


def restart_summary(prefix: str, result: dict) -> str:
    """`Pantheon restarted after a freeze: N of M sessions back`, plus the window names that did
    not come back and any Codex jobs lost. Names only: never an id or a path."""
    if result.get("error"):
        return f"{prefix}, but: {result['error']}"
    resumed = result.get("resumed") or []
    failed = result.get("failed") or []
    expected = result.get("expected")
    if not isinstance(expected, int):
        expected = len(resumed) + len(failed)
    text = f"{prefix}: {len(resumed)} of {expected} sessions back"
    if failed:
        text += "; did not come back: " + ", ".join(f.get("window_name") or "?" for f in failed)
    lost = result.get("codex_lost") or []
    if lost:
        text += "; Codex jobs lost: " + ", ".join(lost)
    return text


class Watcher:
    """One watcher. Every outside effect is injectable so the tests never reach tmux, PowerShell,
    taskkill or the network, and never wait: `run` (tmux), `spawn` (other programs), `sampler`,
    `toast`, `ntfy`, `after_freeze`, `windows_source`, `rows_source`, `clock`, `sleep`, `alive`."""

    def __init__(self, cfg, run: Optional[Callable] = None, spawn: Callable = _default_spawn,
                 sampler: Optional[Callable] = None, toast: Callable = channels.toast,
                 ntfy: Callable = channels.ntfy, after_freeze: Optional[Callable] = None,
                 windows_source: Optional[Callable] = None, rows_source: Optional[Callable] = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 alive: Callable[[int], bool] = _default_alive, kill: Callable = os.kill,
                 proc_root: str = "/proc", root: Optional[Path] = None):
        self.cfg = cfg
        self.h = cfg.health_settings()
        self.run = run or tmuxctl.run
        self.spawn = spawn
        self.sampler = sampler or (lambda winpid, **kw: sample_mod.take_sample(winpid, **kw))
        self.toast = toast
        self.ntfy = ntfy
        self.after_freeze = after_freeze or fullrestart.run_after_freeze
        self.windows_source = windows_source or (lambda: tmuxctl.list_windows(None, tmux=cfg.tools.tmux))
        self.rows_source = rows_source or (lambda windows: fullrestart.current_rows(cfg, windows=windows))
        self.clock = clock
        self.sleep = sleep
        self.alive = alive
        self.kill = kill
        self.proc_root = proc_root
        self.root = Path(root or config_mod.PROJECT_ROOT)

        self.misses = 0
        self.first_miss_at = 0.0      # when the current run of misses began (for a stall's length)
        self.left_frozen = False      # the storm guard left a frozen server for the user
        self.leaving = False
        self.next_snapshot = 0.0
        self.next_sample = 0.0
        self.last_sample: dict = {}
        self.handles_warned = False
        self.memory_warned = False
        self.log_offset: Optional[int] = None
        self.hourly_hour: Optional[str] = None   # UTC hour of the last hourly line; None = not read yet
        self.events_offset: Optional[int] = None
        self.next_empty_check = 0.0
        self.state =_read_json(health_dir(cfg) / STATE_FILE)

    # ------------------------------------------------------------------ small helpers

    def save_state(self) -> None:
        _write_json(health_dir(self.cfg) / STATE_FILE, self.state)

    def tmux(self, *args: str):
        return self.run(*args, tmux=self.cfg.tools.tmux)

    def hhmm(self) -> str:
        return datetime.fromtimestamp(self.clock()).strftime("%H:%M")

    def alert(self, title: str, body: str) -> None:
        """Toast and phone, as `[health] alerts` allows. Lock-screen text only: who, what, when;
        never a path, command, prompt or session id. Never Telegram."""
        log.warning("alert: %s - %s", title, body)
        sent = []
        if "toast" in self.h.alerts:
            try:
                sent.append(("toast", *self.toast(title, body, self.cfg.tools.pwsh)))
            except Exception as exc:          # a sender must never stop the watcher
                sent.append(("toast", False, str(exc)))
        if "ntfy" in self.h.alerts:
            notify = self.cfg.notify_settings()
            try:
                sent.append(("ntfy", *self.ntfy(notify.ntfy_url, notify.ntfy_topic, title, body,
                                                priority="high")))
            except Exception as exc:
                sent.append(("ntfy", False, str(exc)))
        record = {"ts": _iso(self.clock()), "title": title, "body": body,
                  "sent": [{"channel": c, "ok": ok, "detail": d} for c, ok, d in sent]}
        try:
            path = health_dir(self.cfg) / ALERTS_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            log.exception("could not record the alert")

    # ------------------------------------------------------------------ the probe

    def probe(self) -> str:
        try:
            cp = self.tmux("list-sessions")
        except BlockingIOError:
            return SPAWN_FAILED
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.ENOMEM):
                return SPAWN_FAILED
            log.warning("tmux could not be asked: %s", exc)
            return ERROR
        if cp.returncode == 0:
            return ANSWERED
        err = (cp.stderr or "").lower()
        if cp.returncode == 124 or "timed out" in err:
            return TIMEOUT
        if "no server running" in err or "error connecting to" in err:
            return NO_SERVER
        # The server answered with an error of its own: it is not frozen.
        return ANSWERED

    def cycle(self) -> str:
        outcome = self.probe()
        if outcome == ANSWERED:
            if self.misses or self.left_frozen:
                log.info("tmux answers again")
            self.misses = 0
            self.left_frozen = False
            self.healthy()
        elif outcome == TIMEOUT:
            if self.left_frozen:
                return outcome
            if not self.misses:
                self.first_miss_at = self.clock()
            self.misses += 1
            log.warning("tmux did not answer (%d of %d)", self.misses, self.h.freeze_after_misses)
            if self.misses >= self.h.freeze_after_misses:
                self.misses = 0
                self.on_freeze()
        elif outcome == SPAWN_FAILED:
            log.warning("could not start tmux: a low-memory signal")
            self.check_memory(spawn_failed=True)
        elif outcome == NO_SERVER:
            log.info("no tmux server running; the watcher leaves")
            self.leaving = True
        return outcome

    # ------------------------------------------------------------------ while healthy

    def healthy(self) -> None:
        now = self.clock()
        server = sample_mod.refresh_server(self.cfg, run=self.run, proc_root=self.proc_root)
        self.record_pane_exits()
        if now >= self.next_snapshot:
            self.next_snapshot = now + self.h.snapshot_seconds
            self.save_snapshot()
        if now >= self.next_empty_check:
            self.next_empty_check = now + EMPTY_CHECK_SECONDS
            self.close_empty_panes()
        if now >= self.next_sample:
            self.next_sample = now + self.h.sample_seconds
            sample = self.sampler(server.get("winpid"), handle_exe=self.h.handle_exe)
            self.last_sample = sample
            self.check_warnings(sample)
            self.write_health(sample)
            self.record_hourly(server, sample)
            self.check_planned(sample)

    def _parent_pids(self) -> Optional[set]:
        """Every MSYS2 pid that has a child process right now (from `/proc/<pid>/ppid`); None when
        /proc cannot be read, so nothing is closed on a guess."""
        try:
            entries = os.listdir(self.proc_root)
        except OSError:
            return None
        parents = set()
        for name in entries:
            if not name.isdigit():
                continue
            try:
                with open(os.path.join(self.proc_root, name, "ppid"), encoding="ascii") as fh:
                    parents.add(fh.read().strip())
            except OSError:
                if os.path.isdir(os.path.join(self.proc_root, name)):
                    return None          # a live process we could not read: its parent is unknown
        return parents

    def close_empty_panes(self) -> list[str]:
        """Close every pane with no session in it whose window has shown nothing new for
        `close_empty_minutes`. A pane running anything (an agent, the deck's python, a command
        under the shell) is never touched, nor are the deck's own windows. Returns the pane ids
        closed."""
        minutes = self.h.close_empty_minutes
        if minutes <= 0:
            return []
        cp = self.tmux("list-panes", "-a", "-F", EMPTY_FMT)
        if getattr(cp, "returncode", 1) != 0:
            return []
        parents = self._parent_pids()
        if parents is None:
            return []
        now = self.clock()
        closed, seen = [], set()
        for line in (cp.stdout or "").splitlines():
            parts = line.split("|", 4)                   # the window name is last: it may hold "|"
            if len(parts) != 5:
                continue
            pane, pid, dead, activity, rest = parts
            command, _, name = rest.partition("|")
            if pane in seen or name in KEPT_WINDOWS:     # grouped sessions list each pane again
                continue
            seen.add(pane)
            try:
                idle = now - int(activity)
            except ValueError:
                continue
            if idle < minutes * 60:
                continue
            if dead != "1":
                base = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
                base = base[:-4] if base.endswith(".exe") else base
                if base not in EMPTY_SHELLS or pid.strip() in parents:
                    continue
            if getattr(self.tmux("kill-pane", "-t", pane), "returncode", 1) == 0:
                log.info("closed empty pane %s (window %s), unchanged for %d min", pane, name, idle // 60)
                closed.append(pane)
        return closed

    def record_hourly(self, server: dict, sample: dict) -> None:
        """Append one line to `state/health/handles-hourly.jsonl` for the first good sample in each
        UTC hour. A failed sample writes nothing; the next sample in the same hour tries again."""
        if not (sample or {}).get("ok"):
            return
        now = self.clock()
        hour = _iso(now)[:13]
        if self.hourly_hour is None:
            self.hourly_hour = last_hour_written(hourly_path(self.cfg))
        if hour == self.hourly_hour:
            return
        try:
            path = hourly_path(self.cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(hourly_line(now, server, sample)) + "\n")
        except OSError:
            log.exception("could not append the hourly handle line")
            return
        self.hourly_hour = hour

    def save_snapshot(self) -> None:
        try:
            windows = self.windows_source()
            rows = self.rows_source(windows)
            snapshot, problems = fullrestart.build_snapshot(self.cfg, rows)
        except Exception:
            log.exception("could not build the session snapshot")
            return
        for p in problems:
            log.info("snapshot leaves out: %s", p)
        _write_json(last_snapshot_path(self.cfg), snapshot)

    def new_pane_exits(self) -> list[dict]:
        """`PaneExit` events (bin/pane_exit, spec step 3) added to the events file since the last
        pass; the first pass only notes where the file ends."""
        path = Path(self.cfg.events_file)
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if self.events_offset is None:
            self.events_offset = size
            return []
        if size < self.events_offset:         # the file was replaced: read it from the top
            self.events_offset = 0
        if size == self.events_offset:
            return []
        try:
            with open(path, "rb") as fh:
                fh.seek(self.events_offset)
                data = fh.read(min(size - self.events_offset, 2_000_000))
        except OSError:
            return []
        self.events_offset += len(data)
        exits = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            if '"PaneExit"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "PaneExit":
                exits.append(event)
        return exits

    def record_pane_exits(self) -> None:
        """On a nonzero exit code, read Windows' crash records naming claude.exe within 2 minutes
        of the exit into state/health/pane-exits.jsonl (spec step 3)."""
        for event in self.new_pane_exits():
            try:
                code = int(event.get("code"))
            except (TypeError, ValueError):
                continue
            if code == 0:
                continue
            at = _event_time(event.get("ts")) or self.clock()
            minutes = int((self.clock() - at) // 60) + 3
            crashes = []
            try:
                cp = self.spawn([sample_mod.powershell_path(), "-NoProfile", "-Command",
                                 capture_mod.app_crash_script(minutes)], sample_mod.SAMPLE_TIMEOUT_SECONDS)
                found = capture_mod._json_or_text(cp.stdout or "")
                found = [found] if isinstance(found, dict) else found
                for item in found if isinstance(found, list) else []:
                    when = _event_time(item.get("time")) if isinstance(item, dict) else None
                    if when is not None and abs(when - at) <= 120:
                        crashes.append(item)
            except Exception as exc:
                log.warning("could not read the Application log for a pane exit: %s", exc)
            record = {"ts": _iso(at), "tmux_pane": event.get("tmux_pane"), "code": code,
                      "application_events": crashes}
            try:
                path = health_dir(self.cfg) / "pane-exits.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
            except OSError:
                log.exception("could not record the pane exit")

    def health_status(self, sample: dict) -> tuple[str, str]:
        """(level, message) for the deck: "ok" with no message, or "warn"/"bad" with one line."""
        if "deck" not in self.h.alerts:
            return "ok", ""
        handles = sample_mod.server_handles(sample)
        parts, level = [], "ok"
        if self.memory_warned or self.memory_low(sample):
            level = "bad"
            parts.append(f"memory low ({self.memory_reason(sample)})")
        if handles is not None and handles >= self.h.warn_handles:
            if handles >= self.h.planned_restart_handles:
                level = "bad"
            elif level == "ok":
                level = "warn"
            parts.append(f"tmux server holds {handles:,} handles")
        return level, ("health: " + "; ".join(parts)) if parts else ""

    def write_health(self, sample: dict) -> None:
        level, message = self.health_status(sample)
        _write_json(health_path(self.cfg), {
            "written_at": _iso(self.clock()), "level": level, "message": message,
            "server_handles": sample_mod.server_handles(sample),
            "commit_percent": sample.get("commit_percent"),
            "sample": sample,
        })

    # ------------------------------------------------------------------ warnings (sub-step 5)

    def memory_low(self, sample: dict) -> bool:
        pct = (sample or {}).get("commit_percent")
        return pct is not None and pct >= self.h.low_commit_percent

    def memory_reason(self, sample: dict) -> str:
        pct = (sample or {}).get("commit_percent")
        return f"commit {pct:g}% of the limit" if pct is not None else "commit not known"

    def top_users(self, sample: dict, n: int = 3) -> str:
        names = []
        for p in ((sample or {}).get("top") or [])[:n]:
            mb = p.get("private_mb")
            names.append(f"{p.get('name') or '?'} {mb:,.0f} MB" if isinstance(mb, (int, float)) else
                         str(p.get("name") or "?"))
        return ", ".join(names) or "not known"

    def new_log_problems(self) -> list[str]:
        """Lines added to state/pantheon.log since the last pass that say a process could not get
        memory. The first pass only notes where the file ends (old lines are not news)."""
        path = Path(self.cfg.log_file)
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if self.log_offset is None:
            self.log_offset = size
            return []
        if size < self.log_offset:            # the log was replaced: read it from the top
            self.log_offset = 0
        if size == self.log_offset:
            return []
        try:
            with open(path, "rb") as fh:
                fh.seek(self.log_offset)
                data = fh.read(min(size - self.log_offset, 2_000_000))
        except OSError:
            return []
        self.log_offset += len(data)
        return [line for line in data.decode("utf-8", errors="replace").splitlines()
                if _is_low_memory_line(line)]

    def check_warnings(self, sample: dict) -> None:
        """Each warning is sent once per crossing and cleared when back under."""
        handles = sample_mod.server_handles(sample)
        if handles is not None:
            if handles >= self.h.warn_handles:
                if not self.handles_warned:
                    self.handles_warned = True
                    self.alert("Pantheon: tmux handles high",
                               f"The tmux server holds {handles:,} handles (warning at {self.h.warn_handles:,}).")
            else:
                self.handles_warned = False
        self.check_memory(spawn_failed=bool((sample or {}).get("spawn_failed")))

    def check_memory(self, spawn_failed: bool = False) -> None:
        sample = self.last_sample
        reasons = []
        if self.memory_low(sample):
            reasons.append(self.memory_reason(sample))
        if spawn_failed:
            reasons.append("a process could not start")
        if self.new_log_problems():
            reasons.append("a memory allocation failed")
        if not reasons:
            self.memory_warned = False
            return
        if self.memory_warned:
            return
        self.memory_warned = True
        self.alert("Pantheon: memory low",
                   f"Memory is low: {'; '.join(reasons)}. Top users: {self.top_users(sample)}.")

    # ------------------------------------------------------------------ planned restart (sub-step 6)

    def clients_idle(self) -> bool:
        """No attached client has typed for `planned_idle_minutes` (`#{client_activity}` is the
        epoch second of each client's last input). No client at all counts as idle; an
        unanswered question does not."""
        cp = self.tmux("list-clients", "-F", "#{client_activity}")
        if getattr(cp, "returncode", 1) != 0:
            return False
        stamps = []
        for line in (cp.stdout or "").splitlines():
            try:
                stamps.append(int(line.strip()))
            except ValueError:
                continue
        if not stamps:
            return True
        return self.clock() - max(stamps) >= self.h.planned_idle_minutes * 60

    def _next_local_midnight(self) -> float:
        now = datetime.fromtimestamp(self.clock())
        return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()

    def check_planned(self, sample: dict) -> None:
        """The planned restart: when server handles reach
        `planned_restart_handles`, every agent is between turns and no client has typed for
        `planned_idle_minutes`, run the normal full restart. At most once per 6 hours; handles
        back above half the threshold within an hour of one = something else is leaking: alert
        once and stop planning until the next day."""
        if not self.h.planned_restart:
            return
        handles = sample_mod.server_handles(sample)
        if handles is None:
            return
        now = self.clock()
        threshold = self.h.planned_restart_handles
        last = self.state.get("last_planned_restart")
        if (isinstance(last, (int, float)) and now - last < PLANNED_BACKOFF_WINDOW_SECONDS
                and handles >= threshold / 2 and self.state.get("planned_backoff_for") != last):
            self.state["planned_backoff_for"] = last
            self.state["planned_paused_until"] = self._next_local_midnight()
            self.save_state()
            self.alert("Pantheon: handles climbing again",
                       f"tmux handles are back to {handles:,} within an hour of a planned restart; "
                       "planned restarts are off until tomorrow.")
            return
        paused = self.state.get("planned_paused_until")
        if isinstance(paused, (int, float)) and now < paused:
            return
        if handles < threshold:
            return
        if isinstance(last, (int, float)) and now - last < PLANNED_EVERY_SECONDS:
            return
        retry = self.state.get("planned_retry_after")
        if isinstance(retry, (int, float)) and now < retry:
            return
        try:
            rows = self.rows_source(self.windows_source())
        except Exception:
            log.exception("could not read the sessions for a planned restart")
            return
        if fullrestart.waiting_on(rows, self.cfg.tmux_session):
            return
        if not self.clients_idle():
            return
        self.planned_restart(handles)

    def planned_restart(self, handles: int) -> None:
        started = self.clock()
        self.alert("Pantheon: planned restart", f"Planned restart (tmux handles {handles:,})")
        env = dict(os.environ, PANTHEON_SESSION=self.cfg.tmux_session)
        argv = [(self.root / "bin" / "pantheon").as_posix(), "--restart", "--all", "--ready"]
        try:
            code = self.spawn(argv, LAUNCH_TIMEOUT_SECONDS, env=env).returncode
        except Exception as exc:
            log.error("the planned restart could not start: %s", exc)
            code = -1
        if code != 0:
            # Nothing was ended; try again once things have been quiet again.
            self.state["planned_retry_after"] = started + self.h.planned_idle_minutes * 60
            self.save_state()
            self.alert("Pantheon: planned restart", "The planned restart did not go ahead; nothing was ended.")
            return
        self.state["last_planned_restart"] = started
        self.save_state()
        result = self.wait_for_result(started)
        if result is None:
            self.alert("Pantheon: planned restart", "Planned restart started; no result came back yet.")
        else:
            self.alert("Pantheon: planned restart", restart_summary("Pantheon restarted as planned", result))
        self.next_snapshot = self.next_sample = 0.0

    def wait_for_result(self, since: float) -> Optional[dict]:
        """The result file once it is newer than `since`; None if none came within the wait."""
        path = fullrestart.result_path(self.cfg)
        waited = 0.0
        while waited < RESULT_WAIT_SECONDS:
            try:
                if path.stat().st_mtime >= since:
                    return fullrestart.read_result(self.cfg)
            except OSError:
                pass
            self.sleep(RESULT_POLL_SECONDS)
            waited += RESULT_POLL_SECONDS
        return None

    # ------------------------------------------------------------------ freeze (sub-step 4)

    def on_freeze(self) -> None:
        """The freeze actions, in the spec's order: capture, storm guard, alert, end the server,
        restart and resume, alert the result. The restart goes ahead even when the capture
        failed, and the alert says so."""
        now = self.clock()
        hhmm = self.hhmm()
        log.error("tmux did not answer %d probes in a row; capturing, then one last probe",
                  self.h.freeze_after_misses)
        server = sample_mod.read_server(self.cfg)
        # 1. capture
        try:
            folder, manifest = capture_mod.capture(self.cfg, self.h, server, self.spawn,
                                                   self.sampler, self.clock)
        except Exception as exc:              # capture is best effort; the restart is not
            log.exception("the freeze capture failed")
            folder, manifest = None, {"error": str(exc), "taken": [], "skipped": []}
        if folder is None or not manifest.get("taken"):
            note = " The capture failed."
        elif manifest.get("skipped"):
            note = " Some details could not be saved."
        else:
            note = ""
        log.info("freeze capture: %s (%d taken, %d skipped)", folder,
                 len(manifest.get("taken") or []), len(manifest.get("skipped") or []))
        # 1b. ask once more before ending anything. On 2026-10-04 the server stopped answering
        # for about 100 s, answered again while the capture ran, and was killed anyway, taking
        # four running background agents with it. A server that answers now stalled; it did not
        # freeze: keep the details (as stall-*, so the done bar's freeze-* count stays true),
        # say so, and end nothing.
        if self.probe() == ANSWERED:
            self.on_stall(folder, now)
            return
        self.state["last_freeze"] = now
        if not self.h.auto_restart:
            # The spec's freeze actions assume auto_restart; off = capture and alert only.
            self.alert("Pantheon froze", f"Pantheon froze at {hhmm}. Details saved; automatic restart is off.{note}")
            self.left_frozen = True
            self.save_state()
            return
        # 2. storm guard
        last = self.state.get("last_auto_restart")
        if isinstance(last, (int, float)) and now - last < self.h.storm_minutes * 60:
            self.alert("Pantheon froze again", "Pantheon froze again; not restarting; details saved")
            self.left_frozen = True
            self.save_state()
            return
        # 3. alert
        self.alert("Pantheon froze", f"Pantheon froze at {hhmm}. Saving details, then restarting.{note}")
        self.state["last_auto_restart"] = now
        self.save_state()
        # 4. end the server and everything under it
        self.end_server(server)
        # 5. restart and resume
        try:
            result = self.after_freeze(self.cfg)
        except Exception as exc:
            log.exception("the restart after the freeze failed")
            result = {"error": f"the restart failed ({type(exc).__name__})", "resumed": [], "failed": []}
        # 6. alert the result
        self.alert("Pantheon restarted", restart_summary("Pantheon restarted after a freeze", result or {}))
        self.next_snapshot = self.next_sample = 0.0

    def on_stall(self, folder: Optional[Path], now: float) -> None:
        """tmux answered again after the capture: rename the capture folder freeze-* to stall-*,
        record it, alert, and leave the server alone."""
        if folder is not None:
            stalled = folder.with_name("stall-" + folder.name[len("freeze-"):])
            try:
                folder.rename(stalled)
                folder = stalled
            except OSError:
                log.exception("could not rename %s", folder)
        seconds = int(now - self.first_miss_at) if self.first_miss_at else 0
        log.warning("tmux answered again after the capture: a stall of about %d s, not a freeze; "
                    "not restarting (details: %s)", seconds, folder)
        self.state["last_stall"] = now
        self.save_state()
        self.alert("Pantheon stalled",
                   f"Pantheon stopped answering for about {max(seconds, 1)} seconds at {self.hhmm()} "
                   f"and came back by itself. Nothing was restarted; details saved.")
        self.healthy()

    def end_server(self, server: dict) -> None:
        """`taskkill //F //T //PID <winpid>`: the whole tree goes (doubled slashes stop MSYS2
        rewriting the flags). Without a known Windows pid, the MSYS2 pid gets SIGKILL instead."""
        winpid, msys_pid = server.get("winpid"), server.get("msys_pid")
        if winpid:
            exe = TASKKILL if os.path.exists(TASKKILL) else "taskkill"
            try:
                cp = self.spawn([exe, "//F", "//T", "//PID", str(winpid)], KILL_TIMEOUT_SECONDS)
                log.info("taskkill exit %s: %s", cp.returncode, (cp.stdout or cp.stderr or "").strip()[:300])
            except Exception as exc:
                log.error("taskkill failed: %s", exc)
        elif msys_pid:
            try:
                self.kill(int(msys_pid), signal.SIGKILL)
            except (OSError, ValueError) as exc:
                log.error("could not end the server: %s", exc)
        else:
            log.error("the server's pid is not known; it could not be ended")
            return
        if msys_pid:
            waited = 0
            while self.alive(int(msys_pid)) and waited < 10:
                self.sleep(1.0)
                waited += 1

    # ------------------------------------------------------------------ the loop

    def loop(self, stop: Optional[Callable[[], bool]] = None, max_cycles: Optional[int] = None) -> int:
        cycles = 0
        while True:
            try:
                self.cycle()
            except Exception:                 # one bad pass must not end the watcher
                log.exception("watch pass failed")
            cycles += 1
            if self.leaving or (max_cycles is not None and cycles >= max_cycles):
                return cycles
            if stop is not None and stop():
                return cycles
            self.sleep(self.h.probe_seconds)


# --------------------------------------------------------------------------- single instance


def other_watcher(cfg, alive: Callable[[int], bool] = _default_alive) -> Optional[int]:
    """The pid of another live watcher for this state folder, or None."""
    try:
        pid = int(pid_path(cfg).read_text().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid() or pid <= 0:
        return None
    return pid if alive(pid) else None


def claim(cfg) -> bool:
    if other_watcher(cfg) is not None:
        return False
    try:
        path = pid_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def release(cfg) -> None:
    try:
        if int(pid_path(cfg).read_text().strip()) == os.getpid():
            pid_path(cfg).unlink()
    except (OSError, ValueError):
        pass


def main() -> int:
    cfg = fullrestart.with_session_override(config_mod.load())
    try:
        health_dir(cfg).mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(health_dir(cfg) / LOG_FILE), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    except OSError:
        pass
    if not cfg.health_settings().enabled:
        log.info("[health] enabled = false; the watcher does not run")
        return 0
    if not claim(cfg):
        log.info("another watcher is already running")
        return 0
    try:
        os.chdir(config_mod.PROJECT_ROOT)    # the toast script path is relative to the project
    except OSError:
        pass
    signal.signal(signal.SIGTERM, lambda *_: (release(cfg), os._exit(0)))
    log.info("watcher started (pid %s, session %s)", os.getpid(), cfg.tmux_session)
    try:
        Watcher(cfg).loop()
    finally:
        release(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
