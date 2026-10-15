"""bin/pane_exit: one valid JSON line per call, never a failure."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pantheon.events import read_events

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "pane_exit"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def _run(state: Path, *args: str, pane: str = "%7") -> subprocess.CompletedProcess:
    env = dict(os.environ, PANTHEON_STATE_DIR=str(state), TMUX_PANE=pane)
    return subprocess.run([BASH, str(SCRIPT), *args], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=30)


def test_pane_exit_appends_one_valid_json_line(tmp_path):
    for code in ("3", "0"):
        done = _run(tmp_path, code)
        assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    lines = (tmp_path / "agents" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "PaneExit" and first["code"] == 3 and first["tmux_pane"] == "%7"
    assert first["ts"].endswith("Z") and first["ts"][:2] == "20"
    events, errors = read_events(tmp_path / "agents" / "events.jsonl")
    assert errors == 0 and [e.event for e in events] == ["PaneExit", "PaneExit"]
    assert events[0].tmux_pane == "%7" and events[0].extra["code"] == 3


def test_pane_exit_odd_input_still_writes_valid_json(tmp_path):
    assert _run(tmp_path, "abc", pane='%1"}x').returncode == 0
    assert _run(tmp_path).returncode == 0
    lines = (tmp_path / "agents" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(s) for s in lines]
    assert [r["code"] for r in rows] == [None, None]
    assert rows[0]["tmux_pane"] == "%1"


def test_pane_exit_never_fails_when_it_cannot_write(tmp_path):
    blocker = tmp_path / "state"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    done = _run(blocker, "1")
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
