"""Leave when the tmux server this process was started under is gone.

`tmux kill-server` is supposed to end every pane's process. Under MSYS2 it does not: the three apps
from the 07:51 start on 2026-09-02 were still running at 17:41 with 340 seconds of CPU each, after
the server had been killed twice from a phone. Their log shows what happened -- Textual's exit
path raised `OSError: [Errno 5] Input/output error` printing to the dead pty, and a driver thread
still blocked on that pty kept the interpreter alive. Signals cannot be relied on there, so every
long-running Pantheon process checks for itself: the server's pid is the second field of `$TMUX`
(`<socket>,<pid>,<session id>`), `os.kill(pid, 0)` asks whether it exists without spawning anything,
and two misses in a row mean leave -- with `os._exit`, because a graceful exit is exactly what hung.
Outside tmux (`$TMUX` unset) the watch does nothing.

The checks run on their own daemon thread, never on the app's event loop. Four deck/queue/hud processes outlived their server on 2026-09-05/06 because
the first version ran `tick` from Textual's `set_interval`: when the server dies without the pty
hanging up (a Windows-level termination, not `kill-server`), an app that keeps redrawing blocks in
its write to a pty nobody reads, the event loop freezes, and the timer never fires again. Reproduced
under a private tmux server: a redrawing probe logged one `alive=False` check, then no check for
the next 90 seconds while the process stayed up; the same probe idle left on its second miss. The
thread only calls `os.kill` and `os._exit`, so a frozen loop cannot hold it back, and the log line
it writes on the way out is given two seconds, not forever.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from . import tmuxctl

CHECK_SECONDS = 30          # how often an app asks; a dead server is noticed within a minute
MISSES_BEFORE_EXIT = 2      # two misses in a row, so one odd answer never ends a live deck
LOG_GRACE_SECONDS = 2.0     # the longest the way out waits on logging (its lock may be held)

log = logging.getLogger("pantheon.orphan")


def _bounded(fn: Callable[[], None], seconds: float = LOG_GRACE_SECONDS) -> None:
    """Run `fn` on a throwaway daemon thread and wait at most `seconds` for it. Logging takes a
    lock per handler; if a frozen thread holds one, the way out must not wait for it."""
    def run() -> None:
        try:
            fn()
        except Exception:
            pass
    worker = threading.Thread(target=run, name="pantheon-orphan-log", daemon=True)
    worker.start()
    worker.join(seconds)


def exit_hard(code: int = 0) -> None:
    """The last line of every `__main__`: a driver thread blocked on a dead pty must not keep the
    process alive after the app has returned (the 07:51 orphans above)."""
    _bounded(logging.shutdown)
    os._exit(int(code or 0))


class OrphanWatch:
    """`tick()` on a timer; it calls `leave` (default: `exit_hard`) after MISSES_BEFORE_EXIT
    consecutive checks find the server gone. `pid`, `leave` and `alive` are injectable for tests."""

    def __init__(self, pid: Optional[int] = None, leave: Optional[Callable[[], None]] = None,
                 alive: Optional[Callable[[Optional[int]], Optional[bool]]] = None,
                 from_env: bool = True) -> None:
        self.pid = pid if pid is not None or not from_env else tmuxctl.server_pid()
        self.misses = 0
        self._leave = leave or exit_hard
        self._alive = alive or tmuxctl.server_alive

    @classmethod
    def inert(cls) -> "OrphanWatch":
        """A watch that never fires (tests, or a process deliberately run outside tmux)."""
        return cls(pid=None, from_env=False)

    @property
    def active(self) -> bool:
        """False outside tmux: nothing to watch."""
        return self.pid is not None

    def tick(self) -> bool:
        """One check. True when the process is leaving (the leave callback has been called)."""
        if not self.active:
            return False
        alive = self._alive(self.pid)
        if alive is None or alive:
            self.misses = 0
            return False
        self.misses += 1
        if self.misses < MISSES_BEFORE_EXIT:
            return False
        _bounded(lambda: log.info("tmux server %s is gone; leaving", self.pid))
        self._leave()
        return True


def start_watchdog(watch: OrphanWatch, seconds: float = CHECK_SECONDS,
                   sleep: Callable[[float], None] = time.sleep) -> Optional[threading.Thread]:
    """Run `watch.tick` every `seconds` on a daemon thread until it leaves. None when the watch is
    inert (outside tmux). `sleep` is injectable so a test can drive it without waiting."""
    if not watch.active:
        return None

    def loop() -> None:
        while True:
            sleep(seconds)
            try:
                if watch.tick():
                    return
            except Exception:       # one bad check must not end the watching for good
                _bounded(lambda: log.exception("orphan watch check failed"))
    thread = threading.Thread(target=loop, name="pantheon-orphan-watch", daemon=True)
    thread.start()
    return thread


def install(app, seconds: float = CHECK_SECONDS,
            start: Callable[..., Optional[threading.Thread]] = start_watchdog) -> OrphanWatch:
    """Start the watch for a Textual app. Call it from `on_mount`. The checks run on their own
    thread (`start_watchdog`), not `app.set_interval`: a frozen event loop stops its timers, which
    is exactly when the watch is needed (module docstring). `app` is kept in the signature so every
    caller stays as it is; `start` is injectable for tests."""
    watch = OrphanWatch()
    if watch.active:
        start(watch, seconds)
    return watch
