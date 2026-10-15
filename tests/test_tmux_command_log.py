"""The tmux command log: every call leaves a `start` and an `end`
line, a call that hangs past the timeout leaves `end ... timeout`, the file rotates past its size
limit, and `[health] command_log = false` writes nothing. A fake tmux script stands in for the
real one, so no test reaches a tmux server."""
from __future__ import annotations

import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pantheon import config, tmuxctl

# conftest replaces the public entry point to protect live tmux; keep the real wrapper.
_real_run = tmuxctl.run

_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"


def _fake_tmux(tmp_path, body: str):
    script = tmp_path / "fake-tmux"
    script.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    path = tmp_path / "state" / "tmux-commands.log"
    monkeypatch.setattr(tmuxctl, "_command_log_path", path)
    return path


def test_a_normal_call_leaves_start_and_end_with_rc(tmp_path, log_file):
    tmux = _fake_tmux(tmp_path, "sys.stdout.write('0|DECK\\n'); sys.exit(3)")
    result = _real_run("list-windows", "-a", tmux=tmux)
    assert result.returncode == 3 and result.stdout == "0|DECK\n"

    start, end = log_file.read_text().splitlines()
    m = re.fullmatch(rf"({_STAMP}) start (\d+-\d+) (\S+) (\S+) (.*)", start)
    assert m, start
    entry_id = m.group(2)
    assert m.group(3) == f"{__name__}:test_a_normal_call_leaves_start_and_end_with_rc"
    assert m.group(4) == threading.current_thread().name.replace(" ", "_")
    assert m.group(5) == f"{tmux} list-windows -a"
    assert re.fullmatch(rf"{_STAMP} end {re.escape(entry_id)} rc=3 \d+\.\d{{3}}", end), end


def test_a_call_past_the_timeout_leaves_start_and_end_timeout(tmp_path, log_file, monkeypatch):
    monkeypatch.setattr(tmuxctl, "TMUX_TIMEOUT_SECONDS", 0.3)
    tmux = _fake_tmux(tmp_path, "time.sleep(5)")
    result = _real_run("list-windows", tmux=tmux)
    # Behaviour unchanged: a capped call still reads as a failed one.
    assert result.returncode == 124 and result.stderr == "tmux timed out"

    start, end = log_file.read_text().splitlines()
    entry_id = start.split()[2]
    assert start.split()[1] == "start"
    assert re.fullmatch(rf"{_STAMP} end {re.escape(entry_id)} timeout", end), end


def test_ids_are_unique_across_threads(tmp_path, log_file):
    tmux = _fake_tmux(tmp_path, "sys.exit(0)")
    threads = [threading.Thread(target=_real_run, args=("has-session",), kwargs={"tmux": tmux})
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = log_file.read_text().splitlines()
    starts = [ln.split()[2] for ln in lines if ln.split()[1] == "start"]
    ends = [ln.split()[2] for ln in lines if ln.split()[1] == "end"]
    assert len(set(starts)) == 6 and sorted(starts) == sorted(ends)


def test_long_argv_is_cut_at_200_chars(tmp_path, log_file):
    tmux = _fake_tmux(tmp_path, "sys.exit(0)")
    _real_run("send-keys", "x" * 500, tmux=tmux)
    start = log_file.read_text().splitlines()[0]
    command = start.split(" ", 5)[5]
    assert len(command) == 200


def test_the_log_rotates_past_the_size_limit(tmp_path, log_file, monkeypatch):
    old = "old line\n" * 100          # 900 bytes, past the limit; one new start line stays under it
    monkeypatch.setattr(tmuxctl, "COMMAND_LOG_MAX_BYTES", len(old) - 1)
    log_file.parent.mkdir(parents=True)
    log_file.write_text(old)
    tmux = _fake_tmux(tmp_path, "sys.exit(0)")
    _real_run("has-session", tmux=tmux)

    rotated = log_file.with_name("tmux-commands.log.1")
    assert rotated.read_text() == old
    lines = log_file.read_text().splitlines()
    assert [ln.split()[1] for ln in lines] == ["start", "end"]


def test_a_logging_failure_never_breaks_the_call(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(tmuxctl, "_command_log_path", blocker / "tmux-commands.log")
    tmux = _fake_tmux(tmp_path, "sys.exit(0)")
    assert _real_run("has-session", tmux=tmux).returncode == 0


def test_check_true_still_raises_and_logs_the_rc(tmp_path, log_file):
    tmux = _fake_tmux(tmp_path, "sys.exit(1)")
    with pytest.raises(subprocess.CalledProcessError):
        _real_run("has-session", tmux=tmux, check=True)
    assert re.search(r" end \S+ rc=1 ", log_file.read_text())


def test_command_log_false_writes_nothing(tmp_path, monkeypatch):
    toml = tmp_path / "pantheon.toml"
    toml.write_text("[health]\ncommand_log = false\n")
    state = tmp_path / "state"
    monkeypatch.setenv("PANTHEON_STATE_DIR", str(state))
    monkeypatch.setattr(config, "DEFAULT_TOML", toml)
    monkeypatch.setattr(tmuxctl, "_command_log_path", tmuxctl._UNRESOLVED)
    tmux = _fake_tmux(tmp_path, "sys.exit(0)")
    _real_run("has-session", tmux=tmux)
    assert tmuxctl._command_log_path is None
    assert not (state / "tmux-commands.log").exists()


def test_command_log_defaults_on_under_the_state_dir(tmp_path, monkeypatch):
    toml = tmp_path / "pantheon.toml"
    toml.write_text("")
    state = tmp_path / "state"
    monkeypatch.setenv("PANTHEON_STATE_DIR", str(state))
    monkeypatch.setattr(config, "DEFAULT_TOML", toml)
    monkeypatch.setattr(tmuxctl, "_command_log_path", tmuxctl._UNRESOLVED)
    tmux = _fake_tmux(tmp_path, "sys.exit(0)")
    _real_run("has-session", tmux=tmux)
    assert len((state / "tmux-commands.log").read_text().splitlines()) == 2


def test_health_settings_default_and_override():
    assert config.Config().health_settings().command_log is True
    assert config.Config(health={"command_log": False}).health_settings().command_log is False


# ---- bin/pantheon's tmux() helper ------------------------------------------------------------

_LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "pantheon"


def _helper_block() -> str:
    """bin/pantheon from its `tmux()` helper through the command-log setup, without the rest of
    the launcher (which would start a deck)."""
    text = _LAUNCHER.read_text()
    return text[text.index("tmux() {"):text.index("# Known-good tags")]


def _run_helper(tmp_path, toml: str, call: str):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "tmux"
    # Echoes its stdin (must be empty: the helper keeps `</dev/null`) and its args, exits 7.
    fake.write_text("#!/bin/sh\ncat\necho \"out:$*\"\nexit 7\n")
    fake.chmod(0o755)
    root = tmp_path / "root"
    root.mkdir()
    (root / "pantheon.toml").write_text(toml)
    state = tmp_path / "state"
    script = f'ROOT="{root}"\n{_helper_block()}\n{call}\necho "rc=$?"\n'
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "PANTHEON_STATE_DIR": str(state)}
    proc = subprocess.run(["bash", "-c", script], input="keyboard input\n", capture_output=True,
                          text=True, env=env, timeout=30)
    return proc, state / "tmux-commands.log"


def test_launcher_helper_logs_start_and_end_and_keeps_rc_and_null_stdin(tmp_path):
    proc, log = _run_helper(tmp_path, "", "tmux has-session -t pantheon")
    assert proc.stdout == "out:has-session -t pantheon\nrc=7\n"
    start, end = log.read_text().splitlines()
    m = re.fullmatch(rf"{_STAMP} start (\d+-sh1) bin/pantheon main tmux has-session -t pantheon", start)
    assert m, start
    assert re.fullmatch(rf"{_STAMP} end {m.group(1)} rc=7 \d+\.\d{{3}}", end), end


def test_launcher_helper_writes_nothing_when_command_log_is_false(tmp_path):
    proc, log = _run_helper(tmp_path, "[notify]\nenabled = false\n[health]\ncommand_log = false\n",
                            "tmux has-session")
    assert proc.stdout == "out:has-session\nrc=7\n"
    assert not log.exists()


def test_launcher_helper_ignores_command_log_false_in_another_table(tmp_path):
    proc, log = _run_helper(tmp_path, "[other]\ncommand_log = false\n[health]\n", "tmux has-session")
    assert len(log.read_text().splitlines()) == 2
