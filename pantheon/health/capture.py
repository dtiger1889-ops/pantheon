"""The freeze capture: what the watcher saves into
`state/health/freeze-<UTC>/` before it ends a frozen tmux server, because ending it destroys the
only evidence.

Every item has its own time limit and the whole capture is capped at `[health] capture_seconds`;
an item that fails, times out or finds the time used up is listed under `skipped` in
`manifest.json` with the reason, and the rest go ahead. Nothing here raises.

Items: the health sample with handle types; the server's threads and what each waits on; every
tmux command that started and never ended, plus the last 200 lines of the command log; the last 100
agent events; Windows' low-virtual-memory events (System log Id 2004) from the last 2 hours;
`ps -W`; and, only when `[health] procdump_exe` is set, a full memory dump of the server (the
newest 3 dumps are kept).
"""
from __future__ import annotations

import json
import os
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import sample as sample_mod
from . import status as status_mod

COMMAND_LOG = "tmux-commands.log"
COMMAND_TAIL_LINES = 200
EVENT_TAIL_LINES = 100
DUMPS_KEPT = 3
ITEM_CAP_SECONDS = 20
DUMP_CAP_SECONDS = 45
PS = "/usr/bin/ps"

# `__PID__` is replaced with an integer only.
THREADS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$p = Get-Process -Id __PID__
if (-not $p) { '[]'; exit 0 }
@($p.Threads | ForEach-Object {
  $cpu = $null
  if ($_.TotalProcessorTime) { $cpu = [math]::Round($_.TotalProcessorTime.TotalSeconds, 2) }
  $wait = ''
  if ("$($_.ThreadState)" -eq 'Wait') { $wait = "$($_.WaitReason)" }
  [ordered]@{ id = $_.Id; state = "$($_.ThreadState)"; wait_reason = $wait; cpu_s = $cpu }
}) | ConvertTo-Json -Depth 3 -Compress
"""

LOW_MEMORY_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$e = Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 2004; StartTime = (Get-Date).AddHours(-2) }
@($e | ForEach-Object { [ordered]@{ time = $_.TimeCreated.ToString('o'); id = $_.Id; message = $_.Message } }) |
  ConvertTo-Json -Depth 3 -Compress
"""


# Windows Error Reporting's crash records (Application log 1000 = application error, 1001 = fault
# bucket) that name claude.exe. `__MINUTES__` is replaced with an integer only.
APP_CRASH_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$e = Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000, 1001; StartTime = (Get-Date).AddMinutes(-__MINUTES__) }
@($e | Where-Object { $_.Message -match 'claude\.exe' } |
  ForEach-Object { [ordered]@{ time = $_.TimeCreated.ToUniversalTime().ToString('o'); id = $_.Id; message = $_.Message } }) |
  ConvertTo-Json -Depth 3 -Compress
"""


# The exit capture's Windows events: System and Application warnings and errors (levels 1-3) and
# every OpenSSH event, from `__MINUTES__` minutes ago until now. Integers only are substituted.
EXIT_EVENTS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$start = (Get-Date).AddMinutes(-__MINUTES__)
$e = @()
$e += Get-WinEvent -FilterHashtable @{ LogName = 'System'; Level = 1, 2, 3; StartTime = $start }
$e += Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Level = 1, 2, 3; StartTime = $start }
$e += Get-WinEvent -FilterHashtable @{ LogName = 'OpenSSH/Operational'; StartTime = $start }
@($e | Sort-Object TimeCreated | ForEach-Object { [ordered]@{ time = $_.TimeCreated.ToUniversalTime().ToString('o');
  log = $_.LogName; provider = $_.ProviderName; id = $_.Id; level = $_.LevelDisplayName; message = $_.Message } }) |
  ConvertTo-Json -Depth 3 -Compress
"""

MEMORY_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$o = Get-CimInstance Win32_OperatingSystem
[ordered]@{ free_physical_mb = [math]::Round($o.FreePhysicalMemory / 1024); total_physical_mb = [math]::Round($o.TotalVisibleMemorySize / 1024);
  commit_used_mb = [math]::Round(($o.TotalVirtualMemorySize - $o.FreeVirtualMemory) / 1024);
  commit_limit_mb = [math]::Round($o.TotalVirtualMemorySize / 1024) } | ConvertTo-Json -Compress
"""

# Windows' own record of tmux.exe ending (Security log 4689, "A process has exited", with its exit
# status), written only once Process Termination auditing is on (one admin command, run once by
# the user). Fields are read by name from the event XML. `read_error` names a failed
# read of the Security log; with no error, `all_exit_events` = 0 means auditing is off (some program
# exits every few seconds; auditpol needs admin to read the setting, which the watcher does not
# have). Integers only substituted.
TMUX_EXIT_EVENTS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$readError = $null
try { $e = @(Get-WinEvent -FilterHashtable @{ LogName = 'Security'; Id = 4689; StartTime = (Get-Date).AddMinutes(-__MINUTES__) } -ErrorAction Stop) }
catch { $e = @(); if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { $readError = "$($_.Exception.Message)" } }
$rows = @($e | ForEach-Object {
  $d = @{}; foreach ($f in ([xml]$_.ToXml()).Event.EventData.Data) { $d[$f.Name] = $f.'#text' }
  if ($d.ProcessName -like '*\tmux.exe') {
    [ordered]@{ time = $_.TimeCreated.ToUniversalTime().ToString('o'); winpid = [int64]$d.ProcessId; status = $d.Status; process = $d.ProcessName }
  } })
[ordered]@{ all_exit_events = $e.Count; read_error = $readError; tmux_exits = $rows } | ConvertTo-Json -Depth 4 -Compress
"""

EXIT_EVENTS_BEFORE_MINUTES = 5
SERVER_LOG_TAIL_LINES = 2000
EXIT_RECORD_FILE = "server-exits.jsonl"   # written by bin/pantheon's -D parent, one line per exit
EXIT_RECORD_WAIT_SECONDS = 3.0


def exit_events_script(minutes: int) -> str:
    return EXIT_EVENTS_SCRIPT.replace("__MINUTES__", str(max(1, int(minutes))))


def tmux_exit_events_script(minutes: int) -> str:
    return TMUX_EXIT_EVENTS_SCRIPT.replace("__MINUTES__", str(max(1, int(minutes))))


def exit_record(health_dir: Path, msys_pid, wait_seconds: float = EXIT_RECORD_WAIT_SECONDS,
                sleep: Callable[[float], None] = None, clock: Callable[[], float] = None,
                not_before: Optional[float] = None) -> Optional[dict]:
    """The -D parent's line for this server (matched by MSYS2 pid, and ended no earlier than
    `not_before`, the server's last good answer, so a reused pid's old line never matches), or
    None. The parent writes it the moment the server ends,
    which can be a moment after the watcher noticed, so wait briefly."""
    import time
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    path, end = health_dir / EXIT_RECORD_FILE, clock() + wait_seconds
    while True:
        for line in reversed(tail(path, 50)):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if msys_pid is None or str(row.get("msys_pid")) != str(msys_pid):
                continue
            if not_before is not None:
                try:
                    ended = datetime.strptime(str(row.get("ended")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if ended.timestamp() < int(not_before):
                    continue
            return row
        if clock() >= end:
            return None
        sleep(0.2)


def app_crash_script(minutes: int) -> str:
    return APP_CRASH_SCRIPT.replace("__MINUTES__", str(max(1, int(minutes))))


def threads_script(winpid: int) -> str:
    return THREADS_SCRIPT.replace("__PID__", str(int(winpid)))


def tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in deque(fh, maxlen=n)]
    except OSError:
        return []


def open_starts(lines: list[str]) -> list[str]:
    """`start` lines of the command log with no `end` for the same id: the commands that never
    came back. Line shape (step 2): `<UTC ms> start <id> ...` and `<UTC ms> end <id> ...`."""
    started: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        if parts[1] == "start":
            started[parts[2]] = line
        elif parts[1] == "end":
            started.pop(parts[2], None)
    return list(started.values())


def _json_or_text(text: str):
    text = (text or "").strip()
    try:
        return json.loads(text) if text else []
    except ValueError:
        return text


def prune_dumps(health_dir: Path, keep: int = DUMPS_KEPT) -> None:
    dumps = sorted(health_dir.glob("freeze-*/*.dmp"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dumps[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _begin(cfg, health, server: dict, prefix: str, started: float) -> tuple[Optional[Path], dict]:
    """The capture folder `<prefix>-<UTC>` and the manifest it starts with."""
    health_dir = sample_mod.health_dir(cfg)
    stamp = datetime.fromtimestamp(started, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = health_dir / f"{prefix}-{stamp}"
    manifest = {"started": datetime.fromtimestamp(started, timezone.utc).isoformat(),
                "server": server, "cap_seconds": health.capture_seconds, "taken": [], "skipped": [], "empty": []}
    n = 2
    while folder.exists():                    # two captures inside one second keep both
        folder = health_dir / f"{prefix}-{stamp}-{n}"
        n += 1
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        manifest["error"] = f"could not make the capture folder: {exc}"
        return None, manifest
    return folder, manifest


def _finish(folder: Path, manifest: dict, clock: Callable[[], float]) -> tuple[Path, dict]:
    manifest["finished"] = datetime.fromtimestamp(clock(), timezone.utc).isoformat()
    try:
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    except OSError as exc:
        manifest["error"] = f"could not write the manifest: {exc}"
    return folder, manifest


def _taker(folder: Path, manifest: dict, deadline: float, clock: Callable[[], float]):
    """`item(name, filename, cap, take)`: run one item under its own cap and the capture's deadline,
    write what it returns, and list it under taken / skipped / empty in the manifest."""

    def item(name: str, filename: str, cap: float, take: Callable[[float], object]) -> None:
        remaining = deadline - clock()
        if remaining < 1:
            manifest["skipped"].append({"item": name, "reason": "the capture ran out of time"})
            return
        t0 = clock()
        try:
            content = take(min(cap, remaining))
        except subprocess.TimeoutExpired:
            manifest["skipped"].append({"item": name, "reason": "timed out"})
            return
        except Exception as exc:              # one item failing must not stop the rest
            manifest["skipped"].append({"item": name, "reason": f"failed: {exc}"})
            return
        if content is None:
            manifest["empty"].append(name)       # nothing there to take: not a failure
            return
        try:
            text = content if isinstance(content, str) else json.dumps(content, indent=1)
            (folder / filename).write_text(text, encoding="utf-8")
        except OSError as exc:
            manifest["skipped"].append({"item": name, "reason": f"could not write: {exc}"})
            return
        manifest["taken"].append({"item": name, "file": filename, "seconds": round(clock() - t0, 1)})

    return item


def _runner(spawn: Callable):
    def run_out(argv: list[str], timeout: float) -> str:
        cp = spawn(argv, timeout)
        return (getattr(cp, "stdout", "") or "") + (getattr(cp, "stderr", "") or "")
    return run_out


def _command_log_lines(state: Path) -> list[str]:
    return tail(state / (COMMAND_LOG + ".1"), 5000) + tail(state / COMMAND_LOG, 50000)


def _server_log_item(item, server_log: str) -> None:
    def server_log_tail(t: float):
        # Just after a rotation the live file is nearly empty; the minutes before the exit
        # are in `.prev`.
        current = Path(server_log)
        prev = current.with_name(current.name + ".prev")
        lines = tail(current, SERVER_LOG_TAIL_LINES) if current.exists() else []
        if len(lines) < SERVER_LOG_TAIL_LINES and prev.exists():
            lines = (tail(prev, SERVER_LOG_TAIL_LINES) + lines)[-SERVER_LOG_TAIL_LINES:]
        return "\n".join(lines) + "\n" if lines else None
    item("tmux server log tail", "tmux-server-log-tail.txt", ITEM_CAP_SECONDS, server_log_tail)


def capture(cfg, health, server: dict, spawn: Callable, sampler: Callable,
            clock: Callable[[], float], powershell: Optional[str] = None,
            server_log: Optional[str] = None) -> tuple[Optional[Path], dict]:
    """Run the capture. (folder, manifest); folder is None when not even the folder could be made.
    `spawn(argv, timeout)` runs an outside program; `sampler(winpid, **kw)` takes a sample.
    `server_log`: tmux's own log, switched on when the server stopped answering, if any."""
    started = clock()
    state = Path(cfg.state_dir)
    health_dir = sample_mod.health_dir(cfg)
    folder, manifest = _begin(cfg, health, server, "freeze", started)
    if folder is None:
        return None, manifest
    item = _taker(folder, manifest, started + health.capture_seconds, clock)
    run_out = _runner(spawn)
    winpid = server.get("winpid")
    ps_exe = powershell or sample_mod.powershell_path()

    item("sample with handle types", "sample.json", ITEM_CAP_SECONDS,
         lambda t: sampler(winpid, handle_exe=health.handle_exe, with_types=True, timeout=t))
    if winpid:
        item("server threads", "server-threads.json", ITEM_CAP_SECONDS,
             lambda t: _json_or_text(run_out([ps_exe, "-NoProfile", "-Command", threads_script(winpid)], t)))
    else:
        manifest["skipped"].append({"item": "server threads", "reason": "the server's Windows pid is not known"})
    if server_log:
        _server_log_item(item, server_log)
    log_lines = _command_log_lines(state)
    item("open tmux commands", "tmux-commands-open.txt", ITEM_CAP_SECONDS,
         lambda t: "\n".join(open_starts(log_lines)) + "\n" if log_lines else None)
    item("command log tail", "tmux-commands-tail.txt", ITEM_CAP_SECONDS,
         lambda t: "\n".join(log_lines[-COMMAND_TAIL_LINES:]) + "\n" if log_lines else None)
    item("last 100 events", "events-tail.jsonl", ITEM_CAP_SECONDS,
         lambda t: ("\n".join(tail(Path(cfg.events_file), EVENT_TAIL_LINES)) + "\n")
         if Path(cfg.events_file).exists() else None)
    item("low-memory events", "low-memory-events.json", ITEM_CAP_SECONDS,
         lambda t: _json_or_text(run_out([ps_exe, "-NoProfile", "-Command", LOW_MEMORY_SCRIPT], t)))
    item("ps -W", "ps-W.txt", ITEM_CAP_SECONDS,
         lambda t: run_out([PS if os.path.exists(PS) else "ps", "-W"], t))
    if health.procdump_exe and winpid:
        def dump(t: float) -> str:
            out = run_out([health.procdump_exe, "-accepteula", "-ma", str(winpid),
                           str(folder / "tmux-server.dmp")], t)
            prune_dumps(health_dir)
            return out
        item("memory dump", "procdump.txt", DUMP_CAP_SECONDS, dump)
    return _finish(folder, manifest, clock)


def capture_exit(cfg, health, server: dict, spawn: Callable, clock: Callable[[], float],
                 last_answered: float, server_log: Optional[str] = None, planned: bool = False,
                 powershell: Optional[str] = None) -> tuple[Optional[Path], dict]:
    """The exit capture: what the watcher saves into `state/health/exit-<UTC>/` when the server it
    was watching is gone, so there is a record of why. The server
    is already dead, so only what it left behind: the exit code its -D parent recorded and
    Windows' tmux.exe exit events, the end of its own log, the tmux commands that
    started and never ended, the last agent events, the last health sample, Windows' System /
    Application warnings and errors plus every OpenSSH event from 5 minutes before the last good
    answer until now, memory right now, and `ps -W`."""
    started = clock()
    state = Path(cfg.state_dir)
    folder, manifest = _begin(cfg, health, server, "exit", started)
    if folder is None:
        return None, manifest
    manifest["last_answered"] = datetime.fromtimestamp(last_answered, timezone.utc).isoformat()
    manifest["planned"] = planned
    manifest["server_log"] = server_log
    item = _taker(folder, manifest, started + health.capture_seconds, clock)
    run_out = _runner(spawn)
    ps_exe = powershell or sample_mod.powershell_path()

    if server_log:
        _server_log_item(item, server_log)
    else:
        manifest["skipped"].append({"item": "tmux server log tail", "reason": "no server log was known"})
    item("exit record", "exit-record.json", ITEM_CAP_SECONDS,
         lambda t: exit_record(sample_mod.health_dir(cfg), server.get("msys_pid"), min(t, EXIT_RECORD_WAIT_SECONDS),
                               not_before=last_answered))

    def crash_dump(t: float):
        # The -D parent raises the core limit, so a crashed server leaves tmux.exe.core in its
        # folder; move it here so the next crash cannot overwrite it (read with gdb tmux.exe <core>).
        core = sample_mod.health_dir(cfg) / "tmux-log" / "tmux.exe.core"
        if not core.exists():
            return None
        os.replace(core, folder / "tmux.exe.core")
        return f"moved {core} here\n"
    item("crash dump", "crash-dump.txt", ITEM_CAP_SECONDS, crash_dump)
    log_lines = _command_log_lines(state)
    item("open tmux commands", "tmux-commands-open.txt", ITEM_CAP_SECONDS,
         lambda t: "\n".join(open_starts(log_lines)) + "\n" if log_lines else None)
    item("command log tail", "tmux-commands-tail.txt", ITEM_CAP_SECONDS,
         lambda t: "\n".join(log_lines[-COMMAND_TAIL_LINES:]) + "\n" if log_lines else None)
    item("last 100 events", "events-tail.jsonl", ITEM_CAP_SECONDS,
         lambda t: ("\n".join(tail(Path(cfg.events_file), EVENT_TAIL_LINES)) + "\n")
         if Path(cfg.events_file).exists() else None)
    health_file = status_mod.health_path(cfg)
    exit_minutes = int((started - last_answered) / 60) + EXIT_EVENTS_BEFORE_MINUTES + 1
    item("windows tmux exit events", "windows-tmux-exit.json", ITEM_CAP_SECONDS,
         lambda t: _json_or_text(run_out([ps_exe, "-NoProfile", "-Command", tmux_exit_events_script(exit_minutes)], t)))
    item("last health sample", "health.json", ITEM_CAP_SECONDS,
         lambda t: health_file.read_text(encoding="utf-8") if health_file.exists() else None)
    minutes = int((started - last_answered) / 60) + EXIT_EVENTS_BEFORE_MINUTES + 1
    item("windows events", "windows-events.json", ITEM_CAP_SECONDS,
         lambda t: _json_or_text(run_out([ps_exe, "-NoProfile", "-Command", exit_events_script(minutes)], t)))
    item("memory now", "memory.json", ITEM_CAP_SECONDS,
         lambda t: _json_or_text(run_out([ps_exe, "-NoProfile", "-Command", MEMORY_SCRIPT], t)))
    item("ps -W", "ps-W.txt", ITEM_CAP_SECONDS,
         lambda t: run_out([PS if os.path.exists(PS) else "ps", "-W"], t))
    return _finish(folder, manifest, clock)
