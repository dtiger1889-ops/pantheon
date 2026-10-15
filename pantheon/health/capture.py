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


def capture(cfg, health, server: dict, spawn: Callable, sampler: Callable,
            clock: Callable[[], float], powershell: Optional[str] = None) -> tuple[Optional[Path], dict]:
    """Run the capture. (folder, manifest); folder is None when not even the folder could be made.
    `spawn(argv, timeout)` runs an outside program; `sampler(winpid, **kw)` takes a sample."""
    started = clock()
    state = Path(cfg.state_dir)
    health_dir = sample_mod.health_dir(cfg)
    stamp = datetime.fromtimestamp(started, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = health_dir / f"freeze-{stamp}"
    manifest = {"started": datetime.fromtimestamp(started, timezone.utc).isoformat(),
                "server": server, "cap_seconds": health.capture_seconds, "taken": [], "skipped": [], "empty": []}
    n = 2
    while folder.exists():                    # two freezes inside one second keep both captures
        folder = health_dir / f"freeze-{stamp}-{n}"
        n += 1
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        manifest["error"] = f"could not make the capture folder: {exc}"
        return None, manifest
    deadline = started + health.capture_seconds
    winpid = server.get("winpid")
    ps_exe = powershell or sample_mod.powershell_path()

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

    def run_out(argv: list[str], timeout: float) -> str:
        cp = spawn(argv, timeout)
        return (getattr(cp, "stdout", "") or "") + (getattr(cp, "stderr", "") or "")

    item("sample with handle types", "sample.json", ITEM_CAP_SECONDS,
         lambda t: sampler(winpid, handle_exe=health.handle_exe, with_types=True, timeout=t))
    if winpid:
        item("server threads", "server-threads.json", ITEM_CAP_SECONDS,
             lambda t: _json_or_text(run_out([ps_exe, "-NoProfile", "-Command", threads_script(winpid)], t)))
    else:
        manifest["skipped"].append({"item": "server threads", "reason": "the server's Windows pid is not known"})
    log_lines = tail(state / (COMMAND_LOG + ".1"), 5000) + tail(state / COMMAND_LOG, 50000)
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
    manifest["finished"] = datetime.fromtimestamp(clock(), timezone.utc).isoformat()
    try:
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    except OSError as exc:
        manifest["error"] = f"could not write the manifest: {exc}"
    return folder, manifest
