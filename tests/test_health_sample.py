"""The health sampler (pantheon/health/sample.py): parses what PowerShell prints,
tolerates odd output, never raises, and its command carries nothing from state/."""
from __future__ import annotations

import json
import subprocess

from pantheon import config
from pantheon.health import sample

FIXTURE = json.dumps({
    "server": {"pid": 5120, "name": "tmux", "handles": 12345, "threads": 9, "cpu_s": 41.2,
               "private_mb": 88.5},
    "claude": [
        {"pid": 700, "name": "claude", "handles": 83000, "threads": 30, "cpu_s": 900.1,
         "private_mb": 420.0},
        {"pid": 701, "name": "claude", "handles": "1200", "threads": None, "cpu_s": "x"},
    ],
    "commit_limit_mb": 32000.0,
    "commit_used_mb": 29000.0,
    "top": {"pid": 9, "name": "chrome", "handles": 1, "threads": 2, "cpu_s": 3, "private_mb": 999},
})

HANDLE_OUTPUT = """
Handle type summary:
  ALPC Port       : 4
  Event           : 120
  Mutant          : 300
  <Unknown type>  : 2
Total handles: 426
"""


def _cfg(tmp_path):
    return config.Config(state_dir=str(tmp_path / "state"))


def test_parse_a_fixture():
    s = sample.parse_sample("WARNING: something\n" + FIXTURE)
    assert s["ok"] is True
    assert s["server"]["handles"] == 12345 and s["server"]["pid"] == 5120
    assert [p["pid"] for p in s["claude"]] == [700, 701]
    assert s["claude"][1]["handles"] == 1200          # a number sent as text still reads
    assert s["claude"][1]["threads"] is None and s["claude"][1]["cpu_s"] is None
    assert s["commit_percent"] == 90.6
    assert len(s["top"]) == 1 and s["top"][0]["name"] == "chrome"   # a lone object, not a list
    assert sample.server_handles(s) == 12345


def test_parse_tolerates_missing_and_broken_output():
    assert sample.parse_sample("")["ok"] is False
    assert sample.parse_sample("{not json")["ok"] is False
    s = sample.parse_sample("{}")
    assert s["ok"] is True and s["server"] is None and s["claude"] == [] and s["commit_percent"] is None
    s = sample.parse_sample(json.dumps({"server": "odd", "claude": 5, "commit_limit_mb": 0,
                                        "commit_used_mb": 5}))
    assert s["server"] is None and s["claude"] == [] and s["commit_percent"] is None


def test_the_command_carries_no_paths_from_state(tmp_path):
    cfg = _cfg(tmp_path)
    argv = sample.sample_command(4321, powershell="powershell.exe")
    assert argv[:3] == ["powershell.exe", "-NoProfile", "-Command"]
    script = argv[3]
    assert "$sp = 4321" in script
    assert "state" not in script.lower()
    assert cfg.state_dir not in script
    # only an integer ever goes in
    assert "$sp = 0" in sample.sample_script("4321; rm -rf /")


def test_take_sample_passes_null_input_and_a_timeout():
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, FIXTURE, "")

    s = sample.take_sample(5120, run=run)
    assert seen["stdin"] is subprocess.DEVNULL and seen["timeout"] == sample.SAMPLE_TIMEOUT_SECONDS
    assert s["ok"] and s["server_winpid"] == 5120 and s["handle_types"] == {}
    assert s["taken_at"].endswith("Z")


def test_take_sample_never_raises():
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 20)

    def broken(argv, **kw):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    assert sample.take_sample(1, run=slow)["ok"] is False
    s = sample.take_sample(1, run=broken)
    assert s["ok"] is False and s["spawn_failed"] is True


def test_handle_types_only_with_the_tool_set():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        assert kw["stdin"] is subprocess.DEVNULL
        if argv[0] == "handle64.exe":
            return subprocess.CompletedProcess(argv, 0, HANDLE_OUTPUT, "")
        return subprocess.CompletedProcess(argv, 0, FIXTURE, "")

    s = sample.take_sample(5120, run=run, handle_exe="handle64.exe")
    assert s["handle_types"] == {"ALPC Port": 4, "Event": 120, "Mutant": 300, "total": 426}
    assert calls[-1] == ["handle64.exe", "-accepteula", "-nobanner", "-s", "-p", "5120"]
    calls.clear()
    sample.take_sample(5120, run=run, handle_exe="")
    assert len(calls) == 1                       # totals only: no handle64 call


def test_server_pid_is_asked_mapped_and_kept(tmp_path):
    cfg = _cfg(tmp_path)
    proc = tmp_path / "proc" / "77"
    proc.mkdir(parents=True)
    (proc / "winpid").write_text("9001\n")

    def answers(*args, tmux=None, check=False):
        assert args == ("display-message", "-p", "#{pid}")
        return subprocess.CompletedProcess(args, 0, "77\n", "")

    rec = sample.refresh_server(cfg, run=answers, proc_root=str(tmp_path / "proc"))
    assert rec["msys_pid"] == 77 and rec["winpid"] == 9001
    assert sample.read_server(cfg)["winpid"] == 9001

    def frozen(*args, tmux=None, check=False):
        return subprocess.CompletedProcess(args, 124, "", "tmux timed out")

    # a server that does not answer keeps the last known pid
    assert sample.refresh_server(cfg, run=frozen)["winpid"] == 9001
