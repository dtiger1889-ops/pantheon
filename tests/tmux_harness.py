"""Shared throwaway-tmux harness: the `Launcher`, its fixtures and the walk helpers.

Moved out of `tests/test_launcher_tmux.py` so the launcher tests and the
scripted walk suite drive the same isolated server. Isolation is unchanged: `TMUX_TMPDIR` is a
fresh temp directory, `TMUX` is unset, `PANTHEON_SESSION=pantheon-test`, and `PANTHEON_STATE_DIR`
is inside the temp folder. Nothing here ever names the live `pantheon` session, and every tmux
call goes through `Launcher.tmux`, which passes `-S <throwaway socket>`.

Every launcher call writes its output to a file rather than a pipe: a pipe is inherited by the
daemonised tmux server, and `subprocess.run` would then wait for the server to die instead of
returning.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

libtmux = pytest.importorskip("libtmux")

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "pantheon"
SESSION = "pantheon-test"
FAKE_AGENT = ROOT / "tests" / "fixtures" / "fake_agent.sh"


def _tmux_exe() -> str:
    for candidate in ("/usr/bin/tmux", "C:/msys64/usr/bin/tmux.exe"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("tmux") or ""


def _tmux_reachable() -> bool:
    exe = _tmux_exe()
    if not exe or not os.path.exists("/usr/bin/bash"):
        return False
    try:
        return subprocess.run([exe, "-V"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


TMUX = _tmux_exe()


def wait_until(predicate, message: str, timeout: float = 60.0, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting: {message}")
        time.sleep(interval)


class Launcher:
    """One throwaway tmux server plus the `bin/pantheon` that drives it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.tmpdir = tmp / "tmux"
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.logs = tmp / "logs"
        self.logs.mkdir(exist_ok=True)
        self.socket = str(self.tmpdir / f"tmux-{os.geteuid()}" / "default")
        self.state_dir = tmp / "state"
        self.state_dir.mkdir(exist_ok=True)
        self.env = dict(os.environ)
        self.env.pop("TMUX", None)
        self.env["TMUX_TMPDIR"] = str(self.tmpdir)
        self.env["PANTHEON_SESSION"] = SESSION
        # The throwaway deck's apps write their log and their events here, never into the live
        # deck's state folder.
        self.env["PANTHEON_STATE_DIR"] = str(self.state_dir)
        # No Assistant on the throwaway deck: it is a real Claude, and one outlived its server.
        # `assistant_settings()` in pantheon/config.py honours this.
        self.env["PANTHEON_ASSISTANT"] = "off"
        self._runs = 0
        # The outer server exists only to give attached clients a real terminal: a tmux client
        # needs a tty, so each "client" is a pane on a second, unrelated server running
        # `bin/pantheon` with no arguments -- the real attach path.
        self.outer_socket = str(tmp / "outer.sock")
        self._clients = 0

    # -- driving the launcher -------------------------------------------------
    def run(self, *args: str, timeout: int = 240) -> subprocess.CompletedProcess:
        self._runs += 1
        out = self.logs / f"run{self._runs}.log"
        with open(out, "w", encoding="utf-8") as fh:
            cp = subprocess.run([str(LAUNCHER), *args], env=self.env, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        cp.stdout = out.read_text(encoding="utf-8", errors="replace")
        return cp

    # -- asking the throwaway server ------------------------------------------
    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([TMUX, "-S", self.socket, *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    @property
    def server(self):
        """The same throwaway server as a libtmux object, for anything easier to read that way."""
        return libtmux.Server(socket_path=self.socket)

    def windows(self) -> list[tuple[int, str, str]]:
        """[(index, name, pane_pid)] of the deck session, or [] when it is gone.

        The pid, not the pane id: `respawn-window` reuses the pane, so only the process changing
        proves the window was restarted.
        """
        cp = self.tmux("list-windows", "-t", SESSION, "-F",
                       "#{window_index}|#{window_name}|#{pane_pid}")
        if cp.returncode != 0:
            return []
        rows = []
        for line in cp.stdout.splitlines():
            index, name, pid = line.split("|")
            rows.append((int(index), name, pid))
        return rows

    def clients(self) -> list[tuple[str, str, int]]:
        """[(tty, session, current window index)] for every attached client."""
        cp = self.tmux("list-clients", "-F", "#{client_tty}|#{client_session}|#{window_index}")
        if cp.returncode != 0:
            return []
        out = []
        for line in cp.stdout.splitlines():
            tty, session, index = line.split("|")
            out.append((tty, session, int(index)))
        return out

    def where_clients_are(self) -> dict[str, int]:
        return {tty: index for tty, _, index in self.clients()}

    def capture(self, target: str) -> str:
        """The characters on a pane right now."""
        cp = self.tmux("capture-pane", "-p", "-t", target)
        return cp.stdout if cp.returncode == 0 else ""

    def pane_dead(self, target: str) -> bool:
        cp = self.tmux("display-message", "-p", "-t", target, "#{pane_dead}")
        return cp.stdout.strip() != "0"

    def has_session(self) -> bool:
        return self.tmux("has-session", "-t", SESSION).returncode == 0

    def sessions(self) -> list[str]:
        cp = self.tmux("list-sessions", "-F", "#{session_name}")
        return cp.stdout.split() if cp.returncode == 0 else []

    # -- attached clients -----------------------------------------------------
    def attach_client(self) -> None:
        """Attach one more real client by running `bin/pantheon` (no arguments) in a pane."""
        self._clients += 1
        command = (
            f'unset TMUX; export TMUX_TMPDIR="{self.tmpdir}" PANTHEON_SESSION="{SESSION}" '
            f'PANTHEON_ASSISTANT=off; '
            f'exec "{LAUNCHER}"'
        )
        home_export = f'HOME="{self.env["HOME"]}" ' if self.env.get("HOME") != os.environ.get("HOME") else ""
        command = command.replace("PANTHEON_ASSISTANT=off;", f"{home_export}PANTHEON_ASSISTANT=off;", 1)
        args = ["-S", self.outer_socket]
        if self._clients == 1:
            args += ["new-session", "-d", "-x", "200", "-y", "50", "-s", "clients",
                     "-n", f"c{self._clients}", command]
        else:
            args += ["new-window", "-d", "-t", "clients", "-n", f"c{self._clients}", command]
        subprocess.run([TMUX, *args], env=self.env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        wait_until(lambda: len(self.clients()) == self._clients,
                   f"client {self._clients} never attached")

    def kill(self) -> None:
        # `kill-server` alone is not enough on this Windows/MSYS tmux: it tears down the pty
        # but does not reliably reap a pane's process tree, so `pantheon.queue`/`pantheon.hud`
        # (started via the `wrap()` shell in bin/pantheon) can survive it (a leftover pane process
        # from an earlier test held a later `pytest | tail` wrapper's stdout pipe open, hanging it
        # well after pytest itself had already exited 0). Collect
        # every pane pid on both throwaway sockets before the kill, then force-kill any survivor
        # and its children by pid so nothing from this test outlives it.
        pane_pids: list[str] = []
        for socket in (self.socket, self.outer_socket):
            cp = subprocess.run([TMUX, "-S", socket, "list-panes", "-a", "-F", "#{pane_pid}"],
                                env=self.env, capture_output=True, text=True, timeout=10)
            if cp.returncode == 0:
                pane_pids += [pid for pid in cp.stdout.split() if pid]
            subprocess.run([TMUX, "-S", socket, "kill-server"], env=self.env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        for pid in pane_pids:
            # `//T //F`, not `/T /F`: MSYS2 rewrites a single-slash `/T`/`/F` into a Windows
            # path (`T:/`, `F:/`) before exec'ing a native binary like taskkill.exe, so this
            # cleanup has probably never actually torn anything down. The
            # doubled slash tells MSYS's argv translation to leave the flag alone.
            subprocess.run(["taskkill", "//T", "//F", "//PID", pid],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

    # -- walk helpers ---------------------------------------------------------
    def isolate_home(self) -> Path:
        """Point `$HOME` at an empty folder for the throwaway deck. The deck's sessions list
        scans `~/.claude/projects`; with the user's real home a stray Enter reopens (resumes) one
        of their actual conversations in the throwaway server. An empty home also means a
        stand-in can never find a login, so nothing real can run by accident."""
        home = self.tmp / "home"
        (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        self.env["HOME"] = str(home)
        # pantheon.toml names the real provider folders by absolute path, which $HOME does not
        # reach; these env overrides (pantheon/config.py) point the deck's processes here instead.
        self.env["PANTHEON_CLAUDE_HOME"] = str(home / ".claude")
        self.env["PANTHEON_CODEX_HOME"] = str(home / ".codex")
        return home

    # All of these go through `self.tmux`, i.e. the throwaway socket. Targets are pane ids
    # ("%N") or "pantheon-test:<window>" names; never the live "pantheon" session.
    def press(self, target: str, *keys: str, delay: float = 0.15) -> None:
        """`send-keys` one key at a time (tmux key names: Enter, Escape, F12, a, H ...)."""
        for key in keys:
            self.tmux("send-keys", "-t", target, key)
            time.sleep(delay)

    def press_client(self, *keys: str) -> None:
        """Type into the attached client's own terminal (outer server), so tmux's ROOT key
        bindings (F1/F2/F3/F8/F12) fire the way they do from a real keyboard; `press` on the
        inner server bypasses them."""
        for key in keys:
            subprocess.run([TMUX, "-S", self.outer_socket, "send-keys", "-t", "clients:c1", key],
                           env=self.env, capture_output=True, timeout=30)
            time.sleep(0.2)

    def client_window(self) -> int | None:
        where = list(self.where_clients_are().values())
        return where[0] if where else None

    def wait_for_client_window(self, index: int, timeout: float = 15.0) -> None:
        wait_until(lambda: self.client_window() == index,
                   f"client never reached window {index}; it is on {self.client_window()}; "
                   f"windows {self.windows()}", timeout=timeout)

    def type_text(self, target: str, text: str) -> None:
        self.tmux("send-keys", "-t", target, "-l", text)

    def screen(self, target: str) -> str:
        return self.capture(target)

    def wait_for_text(self, target: str, text: str, timeout: float = 15.0) -> str:
        """Poll the pane until `text` shows; fail with the last screen attached."""
        last = {"s": ""}

        def seen():
            last["s"] = self.capture(target)
            return text in last["s"]

        try:
            wait_until(seen, f"{text!r} on {target}", timeout=timeout, interval=0.4)
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n--- screen of {target} ---\n{last['s']}\n--- end ---") from None
        return last["s"]

    def wait_for_gone(self, target: str, text: str, timeout: float = 15.0) -> str:
        last = {"s": ""}

        def gone():
            last["s"] = self.capture(target)
            return text not in last["s"]

        try:
            wait_until(gone, f"{text!r} gone from {target}", timeout=timeout, interval=0.4)
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n--- screen of {target} ---\n{last['s']}\n--- end ---") from None
        return last["s"]

    @property
    def events_path(self) -> Path:
        return self.state_dir / "agents" / "events.jsonl"

    def append_event(self, **fields) -> None:
        """Append one hook-shaped event to the THROWAWAY events.jsonl (never the live one)."""
        path = self.events_path
        assert str(path).startswith(str(self.tmp)), "walk events must land in the temp state dir"
        path.parent.mkdir(parents=True, exist_ok=True)
        base = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "source": "claude", "tool_name": None, "notification_type": None,
            "message": None, "detail": None, "agent_id": None, "permission_mode": "default",
        }
        base.update(fields)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(base) + "\n")

    def _stand_in_program(self) -> str:
        """A copy of bash named `claude.exe`: the deck only treats a window as an agent when the
        pane's running program is called claude/node/codex (`AGENT_COMMANDS`), so the stand-in
        must carry that name. It is bash underneath and never contacts anything."""
        exe = self.tmp / "fakebin" / "claude.exe"
        if not exe.exists():
            exe.parent.mkdir(exist_ok=True)
            shutil.copy2("/usr/bin/bash.exe", exe)
        return str(exe)

    def fake_agent(self, name: str, folder: str | Path, events: list[dict] | None = None,
                   session_id: str | None = None, mode: str = "idle") -> dict:
        """Open a window running the stand-in agent and tell the deck about it.

        The window runs `tests/fixtures/fake_agent.sh` (prints a prompt-like line, echoes what it
        is typed). `events` are extra hook events appended with this window's pane id; a
        `SessionStart` is always written first. Returns {"window": name, "pane": "%N",
        "session_id": ..., "cwd": ...}.
        """
        folder = str(folder)
        Path(folder).mkdir(parents=True, exist_ok=True)
        sid = session_id or f"fake-{name}-0000-0000-000000000000"
        cp = self.tmux("new-window", "-d", "-P", "-F", "#{window_index}|#{pane_id}",
                       "-t", f"{SESSION}:", "-n", name, "-c", folder,
                       f"{self._stand_in_program()} {FAKE_AGENT} {mode}")
        assert cp.returncode == 0, f"fake agent window failed: {cp.stderr}"
        index, pane = cp.stdout.strip().split("|")
        transcript = str(self.tmp / f"{sid}.jsonl")
        Path(transcript).write_text("", encoding="utf-8")
        common = {"session_id": sid, "cwd": folder, "tmux_pane": pane, "transcript_path": transcript}
        self.append_event(event="SessionStart", **common)
        for ev in events or []:
            self.append_event(**{**common, **ev})
        return {"window": name, "index": int(index), "pane": pane, "session_id": sid, "cwd": folder}


@pytest.fixture
def launcher(tmp_path):
    lab = Launcher(tmp_path)
    try:
        yield lab
    finally:
        lab.kill()


@pytest.fixture
def walk_deck(launcher):
    """A started deck with an empty `$HOME`, so nothing of the user's is listed or reopened."""
    launcher.isolate_home()
    launcher.run("--no-attach")
    wait_until(lambda: len(launcher.windows()) >= 3, "the three deck windows never appeared")
    return launcher


@pytest.fixture
def started(launcher):
    """A started deck: `pantheon --no-attach`, waited until windows 0-2 exist."""
    launcher.run("--no-attach")
    wait_until(lambda: len(launcher.windows()) >= 3, "the three deck windows never appeared")
    return launcher
