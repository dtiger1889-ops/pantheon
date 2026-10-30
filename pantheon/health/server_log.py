"""tmux's own server log, kept on and kept small, so a server exit leaves its last lines.

Opt-in: everything here runs only with `[health] server_log = true`, off by default
because `-v` logs every drawn character on tmux's only thread and lagged every window.
With it on, `bin/pantheon` starts the server with `-v` from `state/health/tmux-log/`, so tmux writes
`tmux-server-<pid>.log` there (tmux opens it relative to the server's working folder). A server
started without `-v` (the one running before this change) is switched on by the watcher with
SIGUSR2, which toggles tmux's logging; that log lands in whatever folder that server runs in.

The log grows fast (about 11 KB/s for one pane printing ten lines a second, measured on a throwaway
server), so past `[health] server_log_mb` the watcher renames it to `.prev` (replacing
the older one) and toggles logging off and on, which makes tmux open a fresh file under the old
name. At most two files per server, so while it is on the minutes before an exit are on disk.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Callable, Optional

TOGGLE_GAP_SECONDS = 1.0   # two signals in one event-loop pass may reach tmux as one


def log_dir(health_dir: Path) -> Path:
    return health_dir / "tmux-log"


def server_log_path(msys_pid: int, proc_root: str = "/proc") -> Optional[Path]:
    """Where tmux writes this server's log: its working folder plus `tmux-server-<pid>.log`.
    None when the folder cannot be read (the server is gone, or /proc is not there)."""
    try:
        cwd = os.readlink(f"{proc_root}/{int(msys_pid)}/cwd")
    except (OSError, ValueError):
        return None
    return Path(cwd) / f"tmux-server-{int(msys_pid)}.log"


def is_tmux(msys_pid: int, proc_root: str = "/proc") -> bool:
    """True while `msys_pid` is still a tmux process. Read right before each signal: SIGUSR2's
    default action ends a process, so a server that exited and left its pid to another program
    must never get one."""
    try:
        exe = Path(f"{proc_root}/{int(msys_pid)}/exename").read_text().strip()
    except (OSError, ValueError):
        return False
    return os.path.basename(exe).lower() in ("tmux", "tmux.exe")


def freeze_toggle(state: dict, msys_pid, on: bool, kill: Callable = os.kill,
                  proc_root: str = "/proc") -> Optional[str]:
    """tmux's log only while the server is not answering: on at the first missed probe, off once it answers again, so the lag it brings
    falls only on a server that is already stuck. A frozen server acts on SIGUSR2 only once its
    loop runs again, so the log holds what it did as it came back. `state["freeze_log"]` keeps the
    file for the captures; off removes it unless a capture has taken its tail
    (`state["freeze_log_kept"]`). Returns "on", "off" or None. Never raises."""
    if not msys_pid:
        return None
    pid = int(msys_pid)
    try:
        if on:
            if state.get("freeze_log_pid") == pid or not is_tmux(pid, proc_root):
                return None
            kill(pid, signal.SIGUSR2)
            state["freeze_log_pid"] = pid
            path = server_log_path(pid, proc_root)
            state["freeze_log"] = str(path) if path else None
            state.pop("freeze_log_kept", None)
            return "on"
        if state.get("freeze_log_pid") is None:
            return None
        was = state.pop("freeze_log_pid")
        path, kept = state.pop("freeze_log", None), state.pop("freeze_log_kept", False)
        if was != pid or not is_tmux(pid, proc_root):
            return None                    # another server now: the old one took its log with it
        kill(pid, signal.SIGUSR2)
        if path and not kept:
            try:
                Path(path).unlink()
            except OSError:
                pass
        return "off"
    except OSError:
        return None


def keep(state: dict, msys_pid, cap_mb: int, kill: Callable = os.kill,
         sleep: Callable[[float], None] = lambda s: None, proc_root: str = "/proc") -> Optional[str]:
    """One pass while the server answers. Switches logging on once per server when no log exists,
    rotates past the cap, and remembers the path in `state["server_log"]` (after an exit /proc no
    longer says where it was). Returns what it did ("on", "rotated") or None. Never raises."""
    if not msys_pid:
        return None
    pid = int(msys_pid)
    path = server_log_path(pid, proc_root)
    if path is None:
        return None
    state["server_log"] = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    try:
        if size is None:
            if state.get("server_log_switched_on") == pid:
                return None          # switched on once already; never toggle it back off
            if not is_tmux(pid, proc_root):
                return None
            kill(pid, signal.SIGUSR2)
            state["server_log_switched_on"] = pid
            return "on"
        if cap_mb > 0 and size > cap_mb * 1024 * 1024:
            if not is_tmux(pid, proc_root):
                return None
            os.replace(path, path.with_name(path.name + ".prev"))
            kill(pid, signal.SIGUSR2)     # off: tmux closes the renamed file
            sleep(TOGGLE_GAP_SECONDS)
            if not is_tmux(pid, proc_root):
                return None               # gone during the gap: its log stays off with it
            kill(pid, signal.SIGUSR2)     # on: tmux opens a fresh file under the old name
            return "rotated"
    except OSError:
        return None
    return None
