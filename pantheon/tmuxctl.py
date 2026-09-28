"""The deck drives tmux itself. Thin subprocess wrappers; nothing here parses screen text."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

from .models import TmuxWindow

_CANDIDATES = ("/usr/bin/tmux", "C:/msys64/usr/bin/tmux.exe")
_FMT = "#{window_index}|#{window_name}|#{pane_current_command}|#{pane_current_path}|#{pane_id}|#{session_name}"

# perf sweep:
# `(session, tmux)` -> `(monotonic_timestamp, windows)`. Short-lived so the combined deck's two
# panes -- each polling "every window in every session" on their own ~5s timer, a fraction of a
# second apart -- usually share one subprocess spawn instead of each starting their own.
_WINDOWS_CACHE: dict[tuple, tuple[float, list[TmuxWindow]]] = {}


def tmux_path(preferred: Optional[str] = None) -> str:
    for c in ([preferred] if preferred else []) + list(_CANDIDATES):
        if c and os.path.exists(c):
            return c
    found = shutil.which("tmux")
    return found or "tmux"


# A tmux client that cannot reach its server hangs instead of failing.
# Every call is capped so the deck can never hang with it; a capped call reads as a failed one.
TMUX_TIMEOUT_SECONDS = 5


def run(*args: str, tmux: Optional[str] = None, check: bool = False) -> subprocess.CompletedProcess:
    argv = [tmux_path(tmux), *args]
    try:
        try:
            return subprocess.run(argv, capture_output=True, text=True, check=check,
                                  timeout=TMUX_TIMEOUT_SECONDS)
        except BlockingIOError:
            # Cygwin/MSYS2 fork can fail transiently with EAGAIN; one retry after a short pause is enough, and cheaper than a blank deck.
            import time as _time

            _time.sleep(0.2)
            return subprocess.run(argv, capture_output=True, text=True, check=check,
                                  timeout=TMUX_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "tmux timed out")


def in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def server_pid() -> Optional[int]:
    """The tmux server's pid from `$TMUX` (`<socket>,<pid>,<session id>`); None outside tmux."""
    parts = os.environ.get("TMUX", "").split(",")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def server_alive(pid: Optional[int]) -> Optional[bool]:
    """Whether that server process still exists, asked with `os.kill(pid, 0)` -- no subprocess, so
    it is safe from any thread (pantheon/orphan.py). None when there is no pid to ask about."""
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, OSError):
        return True      # it exists (or the question cannot be answered): never claim it is gone
    return True


def list_windows(session: Optional[str] = None, tmux: Optional[str] = None) -> list[TmuxWindow]:
    args = ["list-windows", "-F", _FMT]
    args += ["-t", session] if session else ["-a"]
    cp = run(*args, tmux=tmux)
    out: list[TmuxWindow] = []
    if cp.returncode != 0:
        return out
    for line in cp.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        try:
            out.append(TmuxWindow(int(parts[0]), parts[1], parts[2], parts[3], parts[4], parts[5]))
        except ValueError:
            continue
    return dedupe_grouped(out)


def dedupe_grouped(windows: list[TmuxWindow]) -> list[TmuxWindow]:
    """One row per pane. The launcher's per-client grouped attach makes sessions like
    `pantheon-4756` that SHARE every window with `pantheon`, so `list-windows -a` reports each pane
    twice under two session names; the supervisor then built two rows keyed `pane:%7` and the deck
    died on Textual's DuplicateKey. Keep the copy whose session name is the base
    name (no `-<digits>` client suffix), else the first seen; panes with no id are left alone."""
    def rank(w: TmuxWindow) -> int:
        name = w.session or ""
        head, _, tail = name.rpartition("-")
        return 1 if (head and tail.isdigit()) else 0

    best: dict[str, TmuxWindow] = {}
    order: list[str] = []
    rest: list[TmuxWindow] = []
    for w in windows:
        if not w.pane_id:
            rest.append(w)
            continue
        if w.pane_id not in best:
            best[w.pane_id] = w
            order.append(w.pane_id)
        elif rank(w) < rank(best[w.pane_id]):
            best[w.pane_id] = w
    return [best[k] for k in order] + rest


def list_windows_cached(session: Optional[str] = None, tmux: Optional[str] = None,
                        max_age_seconds: float = 1.0) -> list[TmuxWindow]:
    """`list_windows()`, but reused for `max_age_seconds` before spawning another `tmux` process.

    Every caller that does not inject its own `window_source` (the supervisor pane, the queue
    pane) now defaults through here instead of `list_windows` directly, so a combined-deck tick --
    both panes wanting "every window in every session" moments apart -- costs one subprocess, not
    two. A second-scale cache is short enough that a real change (a window opened or closed) is
    never stale for longer than the user would wait for the next 5-second refresh anyway.
    """
    import time as _time

    key = (session, tmux)
    now = _time.monotonic()
    cached = _WINDOWS_CACHE.get(key)
    if cached is not None and (now - cached[0]) < max_age_seconds:
        return cached[1]
    windows = list_windows(session, tmux)
    _WINDOWS_CACHE[key] = (now, windows)
    return windows


def group_sessions(session: str, tmux: Optional[str] = None) -> list[str]:
    """`session` plus every session grouped with it. Since 2026-09-03 each attached client
    (the desk, the phone) gets its own grouped session (`bin/pantheon`), so the two stop
    resizing each other's windows; grouped sessions share windows but each has its OWN current
    window, so anything that moves the viewer has to move every session in the group."""
    cp = run("list-sessions", "-F", "#{session_name}|#{session_group}", tmux=tmux)
    if cp.returncode != 0:
        return [session]
    group = None
    rows: list[tuple[str, str]] = []
    for line in cp.stdout.splitlines():
        if "|" not in line:
            continue
        name, grp = line.split("|", 1)
        rows.append((name, grp))
        if name == session:
            group = grp
    if not group:
        return [session]
    out = [name for name, grp in rows if grp == group]
    return out or [session]


def select_window(session: str, index: int, tmux: Optional[str] = None) -> bool:
    """Show window `index` in `session` and in every session grouped with it (the desk's and
    the phone's own sessions), so `j` moves whoever is looking at the deck."""
    ok = False
    for name in group_sessions(session, tmux):
        if run("select-window", "-t", f"{name}:{index}", tmux=tmux).returncode == 0:
            ok = True
    return ok


def list_clients(tmux: Optional[str] = None) -> list[tuple[str, str]]:
    """[(client_tty, session_name)] for every attached client."""
    cp = run("list-clients", "-F", "#{client_tty}|#{client_session}", tmux=tmux)
    out: list[tuple[str, str]] = []
    if cp.returncode != 0:
        return out
    for line in cp.stdout.splitlines():
        if "|" in line:
            tty, sess = line.split("|", 1)
            out.append((tty, sess))
    return out


def list_clients_with_activity(tmux: Optional[str] = None) -> list[tuple[str, str, str]]:
    """[(client_tty, session_name, client_activity)] for every attached client -- `client_activity`
    is tmux's raw epoch-seconds string.
    A second function rather than widening `list_clients`'s format string, so `switch_client`
    (the one existing caller) keeps its two-tuples unchanged."""
    cp = run("list-clients", "-F", "#{client_tty}|#{client_session}|#{client_activity}", tmux=tmux)
    out: list[tuple[str, str, str]] = []
    if cp.returncode != 0:
        return out
    for line in cp.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            out.append((parts[0], parts[1], parts[2]))
    return out


def switch_client(session: str, tmux: Optional[str] = None, from_session: Optional[str] = None) -> bool:
    """Move the viewer to `session`. Without `-c`, tmux picks "the current client", which from a
    subprocess is whichever client it likes -- on the phone that was a nested attach, not the user's
    own client. So: switch EVERY client currently looking at `from_session` (the deck's
    session) explicitly; fall back to tmux's choice only when none is found."""
    if from_session:
        targets = [tty for tty, sess in list_clients(tmux) if sess == from_session]
        if targets:
            ok = True
            for tty in targets:
                ok = run("switch-client", "-c", tty, "-t", session, tmux=tmux).returncode == 0 and ok
            return ok
    return run("switch-client", "-t", session, tmux=tmux).returncode == 0


def kill_window(session: str, index: int, tmux: Optional[str] = None) -> bool:
    return run("kill-window", "-t", f"{session}:{index}", tmux=tmux).returncode == 0


# ---------------------------------------------------------------------- the pit (tmux_pit.py)
# `join-pane`/`break-pane`/`select-layout` are how the pit tiles real, tmux-drawn terminals
# side by side without an in-app terminal-emulation widget. Thin wrappers, same shape as every other call here --
# nothing parses screen text, everything goes through `run`'s 5s cap.


def join_pane(src: str, dst_window: str, tmux: Optional[str] = None) -> bool:
    """Move the pane at `src` (a pane id like `%7`, or a window target) into `dst_window`."""
    return run("join-pane", "-s", src, "-t", dst_window, tmux=tmux).returncode == 0


def break_pane(src: str, dst_name: Optional[str] = None, tmux: Optional[str] = None) -> Optional[int]:
    """Move the pane at `src` back out into its own window, named `dst_name` if given.
    Returns the new window's index, or None if tmux refused (e.g. `src` was the pit's only pane
    left -- there is nothing left to break out)."""
    args = ["break-pane", "-s", src, "-P", "-F", "#{window_index}"]
    if dst_name:
        args += ["-n", dst_name]
    cp = run(*args, tmux=tmux)
    try:
        return int(cp.stdout.strip()) if cp.returncode == 0 else None
    except ValueError:
        return None


def select_layout(target: str, layout: str = "tiled", tmux: Optional[str] = None) -> bool:
    return run("select-layout", "-t", target, layout, tmux=tmux).returncode == 0


def display_message(message: str, target: Optional[str] = None, tmux: Optional[str] = None) -> bool:
    """A one-line tmux status message (`display-message`, no `-p` -- this one is meant to be
    SEEN, not captured). `target` is a pane/window; omitted, it shows on whichever client is
    looking at the current session."""
    args = ["display-message"]
    if target:
        args += ["-t", target]
    args.append(message)
    return run(*args, tmux=tmux).returncode == 0


def list_panes(target: str, tmux: Optional[str] = None) -> list[str]:
    """Every pane id (`%N`) in `target` (a window or session), in tmux's own order."""
    cp = run("list-panes", "-t", target, "-F", "#{pane_id}", tmux=tmux)
    if cp.returncode != 0:
        return []
    return [line for line in cp.stdout.splitlines() if line.strip()]


def new_window(session: str, name: str, cwd: str, command: Optional[str] = None,
               tmux: Optional[str] = None, detached: bool = False) -> Optional[int]:
    """Open a window; return its index (via -P -F) or None. `detached` leaves the viewer where
    it is."""
    args = ["new-window", "-t", session, "-n", name, "-c", cwd, "-P", "-F", "#{window_index}"]
    if detached:
        args.insert(1, "-d")
    if command:
        args.append(command)
    cp = run(*args, tmux=tmux)
    try:
        return int(cp.stdout.strip()) if cp.returncode == 0 else None
    except ValueError:
        return None


def window_index_by_name(session: str, name: str, tmux: Optional[str] = None) -> Optional[int]:
    for w in list_windows(session, tmux):
        if w.name == name:
            return w.index
    return None


def open_or_reuse_shell(session: str, cwd: str, name: str = "shell",
                        tmux: Optional[str] = None) -> Optional[int]:
    """One reusable `shell` window: cd it to `cwd` if it exists, else make it. Pressing `o` many
    times never piles up windows. A tmux login shell lands in HOME, so cd
    explicitly rather than trusting `-c`."""
    index = window_index_by_name(session, name, tmux)
    if index is None:
        index = new_window(session, name, cwd, tmux=tmux)
        if index is None:
            return None
    select_window(session, index, tmux)
    send_text(f"{session}:{index}", f'cd "{cwd}"', tmux=tmux)
    return index


def send_text(target: str, text: str, enter: bool = True, tmux: Optional[str] = None) -> bool:
    """Type literal text into a pane (`send-keys -l`), then Enter. Long prompts go here, never argv."""
    ok = run("send-keys", "-t", target, "-l", text, tmux=tmux).returncode == 0
    if ok and enter:
        ok = run("send-keys", "-t", target, "Enter", tmux=tmux).returncode == 0
    return ok


def display(fmt: str, tmux: Optional[str] = None, target: Optional[str] = None) -> str:
    args = ["display-message", "-p"]
    if target:
        args += ["-t", target]
    cp = run(*args, fmt, tmux=tmux)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def pane_command(target: str, tmux: Optional[str] = None) -> str:
    """`pane_current_command` for a window/pane target, e.g. "bash", "node", "codex"."""
    return display("#{pane_current_command}", tmux=tmux, target=target)


def pane_id(target: str, tmux: Optional[str] = None) -> str:
    return display("#{pane_id}", tmux=tmux, target=target)


def capture(target: str, tmux: Optional[str] = None) -> str:
    """The text on a pane right now (`capture-pane -p`). Used only to spot a dialog that needs
    The user (the one-time trust question); nothing else in the deck reads screen text."""
    cp = run("capture-pane", "-p", "-t", target, tmux=tmux)
    return cp.stdout if cp.returncode == 0 else ""


def wait_for_command(target: str, commands: set[str], timeout: float = 30.0, interval: float = 0.5,
                     tmux: Optional[str] = None, sleep=None) -> bool:
    """Poll until the pane's running program is one of `commands` (lower-cased). False on timeout."""
    import time as _time

    sleep = sleep or _time.sleep
    waited = 0.0
    wanted = {c.lower() for c in commands}
    while waited <= timeout:
        # tmux may report a full Windows path (`C:\...\claude.exe`); compare the bare name.
        running = pane_command(target, tmux).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if running.endswith(".exe"):
            running = running[:-4]
        if running in wanted:
            return True
        sleep(interval)
        waited += interval
    return False
