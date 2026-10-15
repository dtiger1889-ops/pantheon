"""The launcher's control helper consumes no terminal input; attach keeps a real tty."""
import os
from pathlib import Path
import subprocess


LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "pantheon"


def test_control_helper_disconnects_stdin_and_preserves_arguments(tmp_path):
    source = LAUNCHER.read_text()
    # The whole helper function (it spans several lines since the command log); with
    # TMUX_LOG unset, as here, it calls tmux directly.
    lines = source.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("tmux()"))
    last = next(i for i in range(first, len(lines)) if lines[i] == "}")
    helper = "\n".join(lines[first:last + 1])
    fake = tmp_path / "tmux"
    fake.write_text('#!/usr/bin/bash\nIFS= read -r input || input=EOF\nprintf "%s|%s\\n" "$input" "$*"\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:/usr/bin")
    result = subprocess.run(["/usr/bin/bash", "-c", helper + '\ntmux list-windows -t "a b"'],
                            input="terminal-input\n", capture_output=True, text=True, env=env,
                            timeout=5, check=True)
    assert result.stdout == "EOF|list-windows -t a b\n"


def test_interactive_attach_bypasses_control_helper():
    attaches = [line.strip() for line in LAUNCHER.read_text().splitlines()
                if "tmux attach -t" in line and not line.lstrip().startswith("#")]
    assert len(attaches) == 2
    assert all(line.startswith("command tmux attach") and "</dev/null" not in line
               for line in attaches)


def _pane_hold() -> str:
    line = next(l for l in LAUNCHER.read_text().splitlines() if l.startswith("PANE_HOLD="))
    return line[len("PANE_HOLD='"):-1]


def test_every_crash_wrapper_ends_on_the_server_pid_not_the_socket():
    # A server started after a freeze listens on the same socket path, so a socket check answered
    # for the dead server and four wrappers lived on (2026-10-05). All four tails use PANE_HOLD,
    # which runs no tmux client and so reads no terminal input.
    source = LAUNCHER.read_text()
    tails = [l for l in source.splitlines()
             if "press Enter to close" in l and not l.lstrip().startswith("#")]
    assert len(tails) == 4
    assert all("$PANE_HOLD'" in l and "has-session" not in l for l in tails)
    hold = _pane_hold()
    assert "'" not in hold and "tmux" not in hold and 'kill -0 "$P"' in hold


def _run_hold(server_pid: int) -> subprocess.Popen:
    hold = _pane_hold().replace("read -t 30", "read -t 1")
    env = dict(os.environ, TMUX=f"/tmp/fake-socket,{server_pid},0", PATH="/usr/bin")
    # stdin stays an open pipe that never sends a line: nobody presses Enter
    return subprocess.Popen(["/usr/bin/bash", "-c", hold], stdin=subprocess.PIPE, env=env)


def test_crash_wrapper_tail_leaves_when_its_server_pid_is_gone():
    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()
    proc = _run_hold(gone.pid)
    try:
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()


def test_crash_wrapper_tail_stays_while_its_server_lives_and_enter_closes_it():
    proc = _run_hold(os.getpid())
    try:
        try:
            proc.wait(timeout=3)
            raise AssertionError("the tail left while its server was alive")
        except subprocess.TimeoutExpired:
            pass
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        assert proc.wait(timeout=5) == 0
    finally:
        proc.kill()
