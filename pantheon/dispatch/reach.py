"""is the pantheon tmux server reachable from wherever this process runs?

The pantheon tmux server lives in Windows session 0 (sshd spawns it); a native-console or
Desktop-app process in session 1 cannot open a client against its socket directly. But the deck is already read and
driven across that boundary over ssh -- `ssh -o BatchMode=yes localhost
'/usr/bin/tmux list-windows -a'` works from a Desktop-app session. This probe is the same shape,
narrowed to the one yes/no question the router needs: does a `pantheon` session exist on the
far side, right now?

Never hangs, never raises: `BatchMode=yes` refuses any interactive prompt (a host-key question,
a password) instead of waiting on it, `ConnectTimeout` bounds the network handshake, and the
subprocess-level `timeout` is a second, harder cap in case the far side accepts the connection
and then wedges (a hang on list-sessions means the server is wedged).
Any failure -- non-zero exit, timeout, missing `ssh` binary -- reads as False, which is exactly
"fall back to path B" (spec step 5, acceptance 5).
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional

Runner = Callable[..., subprocess.CompletedProcess]

# `ssh` resolves bare on PATH via OpenSSH's client on this box; the full path is a fallback for
# a shell that starts with a stripped PATH
# (an MSYS/tmux-launched session), mirroring `tmuxctl.tmux_path`'s candidate-list shape.
SSH_CANDIDATES = ("ssh", "C:/Windows/System32/OpenSSH/ssh.exe")
REMOTE_TMUX = "/usr/bin/tmux"
DEFAULT_SESSION = "pantheon"
CONNECT_TIMEOUT_SECONDS = 3
# The subprocess-level cap: strictly greater than CONNECT_TIMEOUT_SECONDS so a clean ssh-level
# timeout fires first and this is only the backstop for a wedged server past the handshake.
PROBE_TIMEOUT_SECONDS = 6.0


def ssh_path(preferred: Optional[str] = None) -> str:
    for candidate in ([preferred] if preferred else []) + list(SSH_CANDIDATES):
        if candidate and (candidate == "ssh" or os.path.exists(candidate)):
            return candidate
    return "ssh"


def probe_command(session: str = DEFAULT_SESSION, *, ssh_bin: Optional[str] = None,
                  remote_tmux: str = REMOTE_TMUX,
                  connect_timeout: int = CONNECT_TIMEOUT_SECONDS) -> list[str]:
    """The exact argv run for the probe -- exposed so a test (or a future caller building a
    related ssh call) can assert its shape without spawning anything."""
    return [
        ssh_path(ssh_bin),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "localhost",
        f"{remote_tmux} has-session -t {session}",
    ]


def pantheon_tmux_reachable(session: str = DEFAULT_SESSION, *, ssh_bin: Optional[str] = None,
                            timeout: float = PROBE_TIMEOUT_SECONDS,
                            runner: Optional[Runner] = None) -> bool:
    """True only on a clean exit 0 from `tmux has-session -t <session>` run ON the session-0
    server, reached over ssh. False for everything else, including every failure mode above --
    a session Codex process should never block on this question."""
    argv = probe_command(session, ssh_bin=ssh_bin)
    run = runner or subprocess.run
    try:
        completed = run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0
