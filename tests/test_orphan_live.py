"""The orphan watch against a real process on a real (private) tmux server.

Why: four deck/queue/hud processes outlived their tmux server on 2026-09-05/06 even
though every app ran the orphan watch. The cause, reproduced here: when the server dies WITHOUT the
pty hanging up (a Windows-level termination rather than `kill-server`), an app that keeps redrawing
blocks writing to a pty nobody reads, its event loop freezes, and a watch riding on the app's timer
never checks again. This test starts such a redrawing Textual app on a private socket, terminates
the server the abrupt way, and requires the process to be gone within seconds. It failed (process
still alive) with the timer-based watch and passes with the watchdog thread.

Isolation: a private `-L` socket named for this test run, `TMUX` unset in the child's environment,
and a cleanup that kills only the pids this test started. Skips when tmux, `/proc/<pid>/winpid` or
`taskkill` are not reachable (a Desktop-app session, or a non-MSYS2 Python).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMUX = "/usr/bin/tmux"


def _reachable() -> bool:
    if os.name != "posix" or not os.path.exists(TMUX) or not shutil.which("taskkill"):
        return False
    try:
        return subprocess.run([TMUX, "-V"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = [
    pytest.mark.real_tmux,
    pytest.mark.skipif(not _reachable(), reason="no reachable MSYS2 tmux + taskkill"),
]

APP = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, {root!r})
    from textual.app import App
    from textual.widgets import Static
    from pantheon import orphan

    MARK = {mark!r}

    class Busy(App):
        """Redraws twenty times a second, like a deck that is doing its job."""
        def compose(self):
            yield Static("")

        def on_mount(self):
            orphan.install(self, seconds=1)
            self.n = 0
            self.set_interval(0.05, self.churn)
            with open(MARK, "w") as fh:
                fh.write(str(os.getpid()))

        def churn(self):
            self.n += 1
            self.query_one(Static).update(("busy %d " % self.n) * 400)

    Busy().run()
    orphan.exit_hard(0)
''')


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def test_a_redrawing_app_leaves_after_its_server_is_terminated(tmp_path):
    socket = f"pantheon-test-orphan-{os.getpid()}"
    mark = tmp_path / "app.pid"
    script = tmp_path / "busy_app.py"
    script.write_text(APP.format(root=str(ROOT), mark=str(mark)), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    tmux = [TMUX, "-L", socket]
    app_pid = None
    try:
        subprocess.run([*tmux, "new-session", "-d", "-s", "orphan", "-x", "160", "-y", "45",
                        f'"{sys.executable}" "{script}"'], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        assert _wait(mark.exists, 60), "the app never mounted"
        app_pid = int(mark.read_text())
        server = int(subprocess.run([*tmux, "display", "-p", "#{pid}"], env=env,
                                    capture_output=True, text=True, timeout=10).stdout)
        winpid = Path(f"/proc/{server}/winpid").read_text().strip()
        time.sleep(2)                                   # let it redraw for a while first
        # The abrupt way: TerminateProcess on the server, no hang-up on the pty. `//F`, not `/F`:
        # MSYS2 rewrites a bare `/F` argument for a native program into the path `F:/`.
        subprocess.run(["taskkill", "//F", "//PID", winpid], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
        assert _wait(lambda: not _alive(server), 15), "the private server did not go away"
        # Interval 1 s, two misses: gone within a few seconds; 20 s is slack for a busy box.
        assert _wait(lambda: not _alive(app_pid), 20), (
            f"app {app_pid} outlived its tmux server: the orphan watch never fired")
    finally:
        if app_pid and _alive(app_pid):
            os.kill(app_pid, signal.SIGKILL)
        subprocess.run([*tmux, "kill-server"], env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
