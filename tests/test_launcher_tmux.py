"""The launcher's own test lane: `bin/pantheon` driven against a throwaway tmux server.

Why: two launcher changes shipped from a Claude Desktop-app session, where tmux
cannot be exercised at all, and both broke the desk (a deck that took no arrow keys, then a blank
terminal). These tests run the real `bin/pantheon` -- on a private socket, against a throwaway
session named by `PANTHEON_SESSION` -- so the start and attach paths can never again ship unrun.

They skip themselves when tmux is not reachable, so the rest of the suite still passes from a
Desktop-app session; run them from a Claude session INSIDE the deck's tmux.

Isolation: `TMUX_TMPDIR` points at a fresh temp directory and `TMUX` is unset, so the launcher's
plain `tmux` reaches a server of its own -- the user's live deck is never touched. Every launcher
call writes its output to a file rather than a pipe: a pipe is inherited by the daemonised tmux
server, and `subprocess.run` would then wait for the server to die instead of returning.

Not libtmux's own `server` fixture: its `config_file` fixture writes a `~/.tmux.conf` carrying
`base-index 1`, which would renumber the deck's windows 0/1/2 to 1/2/3.
"""
from __future__ import annotations

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

pytestmark = [
    pytest.mark.real_tmux,
    pytest.mark.skipif(not _tmux_reachable(), reason="no reachable tmux (Desktop-app session)"),
]


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
            f'unset TMUX; export TMUX_TMPDIR="{self.tmpdir}" PANTHEON_SESSION="{SESSION}"; '
            f'exec "{LAUNCHER}"'
        )
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
        # (started via the `wrap` shell in bin/pantheon) can survive it. Collect
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


@pytest.fixture
def launcher(tmp_path):
    lab = Launcher(tmp_path)
    try:
        yield lab
    finally:
        lab.kill()


@pytest.fixture
def started(launcher):
    """A started deck: `pantheon --no-attach`, waited until windows 0-2 exist."""
    launcher.run("--no-attach")
    wait_until(lambda: len(launcher.windows()) >= 3, "the three deck windows never appeared")
    return launcher


def test_no_attach_opens_deck_queue_and_budget(started):
    rows = started.windows()
    assert [(index, name) for index, name, _ in rows][:3] == [
        (0, "DECK"), (1, "QUEUE"), (2, "BUDGET"),
    ]


def test_the_throwaway_deck_writes_into_its_own_state_folder(started):
    """PANTHEON_STATE_DIR is honoured end to end, so a test run never lands in the live deck's
    log or events file."""
    wait_until(lambda: any(started.state_dir.iterdir()), "nothing was written to the test state dir")
    assert (started.state_dir / "agents").exists()


def test_attaching_lands_on_the_deck_window(started):
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    started.tmux("select-window", "-t", f"{SESSION}:3")
    started.attach_client()
    time.sleep(1.0)
    where = started.where_clients_are()
    assert list(where.values()) == [0], f"the client did not land on the deck: {where}"


def test_the_attached_client_sees_the_deck_drawn_and_it_takes_keys(started):
    """The 2026-09-03 breakage in one test: a deck that drew nothing, then took no arrow keys."""
    started.attach_client()
    tty, session, _ = started.clients()[0]
    wait_until(lambda: "PANTHEON" in started.capture(f"{session}:0"),
               "the deck never drew for the attached client")
    for key in ("Down", "Down", "Up", "Tab"):
        started.tmux("send-keys", "-t", f"{session}:0", key)
    time.sleep(1.0)
    assert "PANTHEON" in started.capture(f"{session}:0"), "the deck stopped drawing after arrow keys"
    assert not started.pane_dead(f"{session}:0"), "the deck pane died"
    assert tty  # the client is still there to see it


def test_a_second_client_does_not_move_the_first(started):
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    started.attach_client()
    first_tty, first_session, _ = started.clients()[0]
    # The user is looking at his agent in window 3 when the phone attaches.
    started.tmux("switch-client", "-c", first_tty, "-t", f"{first_session}:3")
    wait_until(lambda: started.where_clients_are().get(first_tty) == 3,
               "the first client never reached window 3")
    started.attach_client()
    time.sleep(1.0)
    where = started.where_clients_are()
    assert where[first_tty] == 3, f"the second client dragged the first to window {where[first_tty]}"
    assert sorted(where.values()) == [0, 3], f"the new client did not land on the deck: {where}"


def test_the_back_to_deck_key_is_bound_root_table_no_prefix(started):
    """the configured back key (F12 default) is bound in tmux's ROOT key
    table (no prefix), straight to `select-window -t :=DECK` -- the same binding shape as F1/F2/F3,
    which is what makes it work from inside a stock agent window (a root-table binding is
    intercepted by tmux before the pane ever sees the keystroke).

    `send-keys` cannot exercise this end-to-end: it writes bytes straight into a pane's pty as
    if the program running there received them, bypassing tmux's own client-side key-table
    lookup entirely -- so it can prove a key an APP handles (`Down`/`Tab`, the other test in this
    file), never a root-table tmux binding. Whether F12 itself actually reaches tmux from a real
    terminal is the acceptance's live-walk half -- `spikes/phone_client.py` is the tool for that, not this throwaway-server lane.
    """
    cp = started.tmux("list-keys", "-T", "root")
    assert cp.returncode == 0
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("bind-key -T root F12") and "select-window -t :=DECK" in ln for ln in lines), \
        f"F12 was not bound root-table to the deck window: {lines}"
    # F1 keeps its own, independent binding to the same target.
    assert any(ln.startswith("bind-key -T root F1 ") and "select-window -t :=DECK" in ln for ln in lines)


def test_the_back_to_deck_key_follows_pantheon_toml(launcher, tmp_path):
    """A different `[keys] back_to_deck` value in pantheon.toml changes which key tmux binds --
    proves the launcher actually reads the config rather than hard-coding F12."""
    toml_path = ROOT / "pantheon.toml"
    # Byte-exact read/write (`newline=""`): this is a real, tracked file -- restoring it must not introduce a CRLF/LF churn the next `git status` would see.
    original = toml_path.read_bytes() if toml_path.exists() else None
    try:
        text = (original.decode("utf-8") if original else "") + '\n[keys]\nback_to_deck = "F9"\n'
        toml_path.write_text(text, encoding="utf-8", newline="")
        launcher.run("--no-attach")
        wait_until(lambda: len(launcher.windows()) >= 3, "the three deck windows never appeared")
        cp = launcher.tmux("list-keys", "-T", "root")
        lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
        assert any(ln.startswith("bind-key -T root F9") and "select-window -t :=DECK" in ln for ln in lines), \
            f"F9 was not bound after [keys] back_to_deck = F9: {lines}"
    finally:
        if original is not None:
            toml_path.write_bytes(original)
        else:
            toml_path.unlink(missing_ok=True)


def test_window_names_are_uppercase_role_labels_not_program_paths(started):
    """`tmux list-windows` shows DECK/QUEUE/BUDGET, never a program path."""
    names = {name for _index, name, _pane in started.windows()}
    assert {"DECK", "QUEUE", "BUDGET"} <= names
    assert not any("python" in n.lower() or "\\" in n or "/" in n for n in names)


def test_restart_keeps_agent_windows_and_respawns_the_deck(started):
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    before = {index: pane for index, _, pane in started.windows()}
    started.run("--restart")
    wait_until(lambda: len(started.windows()) >= 4, "windows disappeared over --restart")
    after = {index: (name, pane) for index, name, pane in started.windows()}
    assert 3 in after and after[3][1] == before[3], "the agent window did not survive --restart"
    for index, name in ((0, "DECK"), (1, "QUEUE"), (2, "BUDGET")):
        assert after[index][0] == name
        assert after[index][1] != before[index], f"window {index} was not respawned"


def test_restart_all_removes_everything_then_starts_fresh(started):
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    before = {index: pid for index, _, pid in started.windows()}
    started.run("--restart", "--all")
    wait_until(lambda: len(started.windows()) >= 3, "the deck never came back after --restart --all")
    after = {index: (name, pid) for index, name, pid in started.windows()}
    assert 3 not in after, "the agent window survived --restart --all"
    assert [(index, name) for index, (name, _) in sorted(after.items())] == [
        (0, "DECK"), (1, "QUEUE"), (2, "BUDGET"),
    ]
    for index in (0, 1, 2):
        assert after[index][1] != before[index], f"window {index} was not started fresh"
