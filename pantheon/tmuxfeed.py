"""The tmux feed: one helper process asks tmux what the deck needs and writes it to a file.

Why: the deck and the queue
window called tmux on their drawing thread every 5 seconds -- `list-windows`, then a
`capture-pane` per agent for the startup-question and permission scans. Each call starts a
process, about 60 ms under MSYS2 and up to 0.5 s, and a key pressed meanwhile waits
(578 such calls in 30 minutes, 36.5 s blocked, measured). The
calls cannot simply move to a worker thread: forks from worker threads have hung under MSYS2
(forks from worker threads may hang). So they move to their own
single-threaded process, the way the usage collector did (`hud/collector.py`).

The helper writes `state/tmux-feed.json` every `INTERVAL_SECONDS`: every window (the
`tmuxctl.list_windows` rows) and the screen text of every pane outside the deck's own windows.
Readers (`windows_or_live`, `capture_or_live`) use it while it is fresh and fall back to the old
live call when it is missing or stale, so a dead helper costs speed, never correctness. Actions
that must see the screen NOW (answering a prompt, the reconnect key) keep calling tmux directly.

The deck starts the helper (`ensure_running`) and checks on it every half minute; one helper per
state folder (pid file). It leaves when the deck that started it is gone or the tmux server is.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from . import config as config_mod
from . import orphan
from . import tmuxctl
from .models import TmuxWindow

log = logging.getLogger(__name__)

FEED_NAME = "tmux-feed.json"
PID_NAME = "tmux-feed.pid"
INTERVAL_SECONDS = 2.0
FRESH_SECONDS = 8.0           # older than this and readers go back to asking tmux themselves
CAPTURE_SECONDS = 4.0         # how often each pane's screen is re-read
# The deck's own windows: nothing there is an agent to scan.
SKIP_WINDOWS = frozenset({"DECK", "QUEUE", "BUDGET", "NOTES", "PIT", "gov"})


def feed_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / FEED_NAME


def pid_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / PID_NAME


# --------------------------------------------------------------------------- reading


def read(cfg: config_mod.Config, now: Optional[float] = None) -> Optional[dict]:
    """The feed when it is fresh, else None. Never raises."""
    try:
        data = json.loads(feed_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    at = data.get("at") if isinstance(data, dict) else None
    if not isinstance(at, (int, float)) or now - at > FRESH_SECONDS:
        return None
    return data


def windows_or_live(cfg: config_mod.Config, live: Callable[[], list]) -> list:
    """The feed's windows, or `live()` when there is no fresh feed."""
    data = read(cfg)
    if data is None:
        return live()
    out = []
    for w in data.get("windows") or []:
        try:
            out.append(TmuxWindow(**w))
        except TypeError:
            return live()
    return out


def capture_or_live(cfg: config_mod.Config, live: Callable[..., str] = tmuxctl.capture
                    ) -> Callable[..., str]:
    """A drop-in for `tmuxctl.capture(target, tmux=...)` that reads the feed's screen for that
    window when it has a fresh one. `target` is `session:index` or a pane id."""
    def capture(target: str, tmux: Optional[str] = None) -> str:
        data = read(cfg)
        if data is not None:
            pane = target if target.startswith("%") else _pane_for(data, target)
            shot = (data.get("captures") or {}).get(pane) if pane else None
            if isinstance(shot, dict) and time.time() - float(shot.get("at", 0)) <= FRESH_SECONDS:
                return str(shot.get("text") or "")
        return live(target, tmux=tmux)
    return capture


def _pane_for(data: dict, target: str) -> Optional[str]:
    session, _, index = target.rpartition(":")
    for w in data.get("windows") or []:
        if str(w.get("index")) == index and (not session
                                             or _base(w.get("session", "")) == _base(session)):
            return w.get("pane_id") or None
    return None


def _base(session: str) -> str:
    head, _, tail = session.rpartition("-")
    return head if head and tail.isdigit() else session


# --------------------------------------------------------------------------- writing


def collect_once(cfg: config_mod.Config, captures: dict, tmux: Optional[str] = None,
                 now: Optional[Callable[[], float]] = None,
                 list_windows: Callable = tmuxctl.list_windows,
                 capture: Callable = tmuxctl.capture) -> dict:
    """One pass: list every window, re-read the screens that are due, write the feed. `captures`
    (pane id -> {"at", "text"}) is carried between passes so each pane is read at most once
    every CAPTURE_SECONDS."""
    clock = now or time.time
    windows = list_windows(None, tmux)
    live_panes = set()
    for w in windows:
        if not w.pane_id or w.name in SKIP_WINDOWS:
            continue
        live_panes.add(w.pane_id)
        shot = captures.get(w.pane_id)
        if shot is None or clock() - shot["at"] >= CAPTURE_SECONDS:
            captures[w.pane_id] = {"at": clock(), "text": capture(w.pane_id, tmux=tmux)}
    for gone in set(captures) - live_panes:
        captures.pop(gone, None)
    data = {"at": clock(), "pid": os.getpid(), "windows": [asdict(w) for w in windows],
            "captures": captures}
    _write(feed_path(cfg), data)
    return data


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- the helper process


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def running_pid(cfg: config_mod.Config) -> Optional[int]:
    try:
        pid = int(pid_path(cfg).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def ensure_running(cfg: config_mod.Config, popen: Callable = subprocess.Popen) -> Optional[int]:
    """Start the helper unless one is already running for this state folder. Called by the deck
    at start and every half minute (a check of a pid file, no fork) -- so a helper that left with
    an old deck is replaced by the new deck's. Returns the helper's pid, or None if it could not
    start. Only ever from the main thread (module docstring)."""
    pid = running_pid(cfg)
    if pid is not None:
        return pid
    try:
        proc = popen([sys.executable, "-m", "pantheon.tmuxfeed", "--parent", str(os.getpid())],
                     cwd=str(config_mod.PROJECT_ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("tmux feed did not start: %r", exc)
        return None
    try:
        pid_path(cfg).write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass
    return proc.pid


def loop(cfg: config_mod.Config, parent: Optional[int], tmux: Optional[str] = None,
         sleep: Callable[[float], None] = time.sleep, collect: Callable = collect_once,
         watch: Optional[orphan.OrphanWatch] = None, max_passes: Optional[int] = None) -> int:
    """Passes until the deck that started this is gone or the tmux server is; returns the count."""
    watch = watch if watch is not None else orphan.OrphanWatch()
    captures: dict = {}
    passes = 0
    while True:
        if parent is not None and not _pid_alive(parent):
            log.info("tmux feed leaving: the deck that started it is gone")
            return passes
        try:
            collect(cfg, captures, tmux=tmux)
        except Exception:                          # one bad pass must not end the feed
            log.exception("tmux feed pass failed")
        passes += 1
        if max_passes is not None and passes >= max_passes:
            return passes
        if watch.active and watch.tick():
            return passes
        sleep(INTERVAL_SECONDS)


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parent = int(argv[argv.index("--parent") + 1]) if "--parent" in argv else None
    cfg = config_mod.load()
    try:
        config_mod.ensure_state_dirs(cfg)
        logging.basicConfig(filename=str(cfg.log_file), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    except OSError:
        pass
    try:
        pid_path(cfg).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    log.info("tmux feed started (pid %s, deck %s)", os.getpid(), parent)
    try:
        return loop(cfg, parent, tmux=cfg.tools.tmux) and 0
    finally:
        try:
            if pid_path(cfg).read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path(cfg).unlink()
        except OSError:
            pass


if __name__ == "__main__":
    orphan.exit_hard(main())
