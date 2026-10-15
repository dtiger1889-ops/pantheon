"""Health sampler: a read-only look at the tmux server and every
`claude.exe` from Windows' side.

One `powershell.exe -NoProfile -Command` call (null input, 20 s cap) prints one JSON object: for
the tmux server's Windows pid and every `claude.exe`, the handle count, thread count and CPU
seconds; memory commit in use and the commit limit (`Win32_OperatingSystem`); and the five
processes using the most private memory. Handle counts by type (Event, Mutant = a Windows mutex)
come only from Sysinternals `handle64.exe`, when `[health] handle_exe` names it.

The server's Windows pid is asked of tmux (`display-message -p '#{pid}'`, then MSYS2's
`/proc/<pid>/winpid`) while tmux answers, and kept in `state/health/server.json`: a frozen server
cannot be asked, and the watcher needs that pid most exactly then.

Nothing here raises: a sample that could not be taken comes back with `ok: False` and an `error`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .. import tmuxctl

POWERSHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
SAMPLE_TIMEOUT_SECONDS = 20
HANDLE_TIMEOUT_SECONDS = 20
SERVER_FILE = "server.json"

# `__SERVER_PID__` is replaced with an integer (0 = not known) and nothing else: the script never
# carries a path or any text read from a file.
_SAMPLE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$sp = __SERVER_PID__
function P($p) {
  if (-not $p) { return $null }
  $cpu = $null
  if ($p.TotalProcessorTime) { $cpu = [math]::Round($p.TotalProcessorTime.TotalSeconds, 1) }
  [ordered]@{ pid = $p.Id; name = $p.ProcessName; handles = $p.HandleCount;
              threads = $p.Threads.Count; cpu_s = $cpu;
              private_mb = [math]::Round($p.PrivateMemorySize64 / 1MB, 1) }
}
$o = [ordered]@{}
$o.server = $null
if ($sp -gt 0) { $o.server = P (Get-Process -Id $sp) }
$o.claude = @(Get-Process -Name claude | ForEach-Object { P $_ })
$os = Get-CimInstance Win32_OperatingSystem
if ($os) {
  $o.commit_limit_mb = [math]::Round($os.TotalVirtualMemorySize / 1KB, 1)
  $o.commit_used_mb = [math]::Round(($os.TotalVirtualMemorySize - $os.FreeVirtualMemory) / 1KB, 1)
}
$o.top = @(Get-Process | Sort-Object PrivateMemorySize64 -Descending | Select-Object -First 5 |
           ForEach-Object { P $_ })
$o | ConvertTo-Json -Depth 4 -Compress
"""


def powershell_path() -> str:
    if os.path.exists(POWERSHELL):
        return POWERSHELL
    return shutil.which("powershell.exe") or shutil.which("powershell") or POWERSHELL


def _pid_int(pid: Any) -> int:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def sample_script(server_winpid: Optional[int]) -> str:
    return _SAMPLE_SCRIPT.replace("__SERVER_PID__", str(_pid_int(server_winpid)))


def sample_command(server_winpid: Optional[int], powershell: Optional[str] = None) -> list[str]:
    return [powershell or powershell_path(), "-NoProfile", "-Command", sample_script(server_winpid)]


# --------------------------------------------------------------------------- parsing


def _num(value: Any, kind=int) -> Optional[Any]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return kind(value)
    except (TypeError, ValueError):
        try:
            return kind(float(value))
        except (TypeError, ValueError):
            return None


def _proc(data: Any) -> Optional[dict]:
    """One process record with every field present (None where Windows gave nothing usable)."""
    if not isinstance(data, dict):
        return None
    return {
        "pid": _num(data.get("pid")),
        "name": str(data.get("name") or "?"),
        "handles": _num(data.get("handles")),
        "threads": _num(data.get("threads")),
        "cpu_s": _num(data.get("cpu_s"), float),
        "private_mb": _num(data.get("private_mb"), float),
    }


def _procs(data: Any) -> list[dict]:
    if isinstance(data, dict):        # PowerShell unwraps a one-element array
        data = [data]
    if not isinstance(data, list):
        return []
    return [p for p in (_proc(d) for d in data) if p is not None]


def parse_sample(text: str) -> dict:
    """The JSON the script printed, normalised. Tolerates missing or odd fields, text before the
    JSON (a PowerShell warning), and a lone process where a list was expected."""
    raw = (text or "").lstrip("\ufeff")
    start = raw.find("{")
    try:
        data = json.loads(raw[start:]) if start >= 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"ok": False, "error": "the sample printed no JSON", "server": None, "claude": [],
                "top": [], "commit_used_mb": None, "commit_limit_mb": None, "commit_percent": None}
    used = _num(data.get("commit_used_mb"), float)
    limit = _num(data.get("commit_limit_mb"), float)
    percent = round(100.0 * used / limit, 1) if used is not None and limit else None
    return {
        "ok": True,
        "error": "",
        "server": _proc(data.get("server")),
        "claude": _procs(data.get("claude")),
        "top": _procs(data.get("top")),
        "commit_used_mb": used,
        "commit_limit_mb": limit,
        "commit_percent": percent,
    }


_HANDLE_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _/.-]*?)\s*:\s*(\d+)\s*$")


def parse_handle_types(text: str) -> dict[str, int]:
    """`handle64 -s` summary lines (`  Mutant          : 12`) as {type: count}; the `Total
    handles` line becomes `total`."""
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        m = _HANDLE_LINE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        if name.lower() in ("total handles", "total"):
            out["total"] = int(m.group(2))
        else:
            out[name] = int(m.group(2))
    return out


# --------------------------------------------------------------------------- taking a sample


def handle_types(handle_exe: str, pid: Optional[int], run: Callable = subprocess.run,
                 timeout: float = HANDLE_TIMEOUT_SECONDS) -> dict:
    """Handle counts by type for one process; {} when there is no tool, no pid, or it failed."""
    pid = _pid_int(pid)
    if not handle_exe or not pid:
        return {}
    try:
        cp = run([handle_exe, "-accepteula", "-nobanner", "-s", "-p", str(pid)],
                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_handle_types(getattr(cp, "stdout", "") or "")


def take_sample(server_winpid: Optional[int], run: Callable = subprocess.run, handle_exe: str = "",
                with_types: bool = True, timeout: float = SAMPLE_TIMEOUT_SECONDS,
                powershell: Optional[str] = None) -> dict:
    """One sample, stamped. Never raises."""
    taken_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        cp = run(sample_command(server_winpid, powershell), stdin=subprocess.DEVNULL,
                 capture_output=True, text=True, timeout=timeout, check=False)
        sample = parse_sample(getattr(cp, "stdout", "") or "")
    except subprocess.TimeoutExpired:
        sample = parse_sample("")
        sample["error"] = f"the sample took longer than {timeout:g} s"
    except (OSError, subprocess.SubprocessError) as exc:
        sample = parse_sample("")
        sample["error"] = f"the sample could not start: {exc}"
        sample["spawn_failed"] = isinstance(exc, OSError)
    sample["taken_at"] = taken_at
    sample["server_winpid"] = _pid_int(server_winpid) or None
    sample["handle_types"] = handle_types(handle_exe, server_winpid, run=run) if with_types else {}
    return sample


def server_handles(sample: dict) -> Optional[int]:
    server = (sample or {}).get("server") or {}
    return server.get("handles") if isinstance(server, dict) else None


# --------------------------------------------------------------------------- the server's pid


DEFAULT_SESSION = "pantheon"


def health_dir(cfg) -> Path:
    """`state/health/` for the live session; `state/health/<session>/` for any other (a throwaway
    test deck), so a second watcher never shares the live one's pid file or server pid."""
    base = Path(cfg.state_dir) / "health"
    session = getattr(cfg, "tmux_session", DEFAULT_SESSION) or DEFAULT_SESSION
    return base if session == DEFAULT_SESSION else base / session


def server_path(cfg) -> Path:
    return health_dir(cfg) / SERVER_FILE


def ask_server_pid(cfg, run: Optional[Callable] = None) -> Optional[int]:
    """The tmux server's own (MSYS2) pid, asked of the server; None when it did not answer."""
    run = run or tmuxctl.run
    try:
        cp = run("display-message", "-p", "#{pid}", tmux=cfg.tools.tmux)
    except OSError:
        return None
    if getattr(cp, "returncode", 1) != 0:
        return None
    return _pid_int((getattr(cp, "stdout", "") or "").strip()) or None


def winpid_of(msys_pid: Optional[int], proc_root: str = "/proc") -> Optional[int]:
    """MSYS2's `/proc/<pid>/winpid`: the Windows pid behind an MSYS2 pid."""
    if not msys_pid:
        return None
    try:
        return _pid_int(Path(proc_root, str(msys_pid), "winpid").read_text().strip()) or None
    except OSError:
        return None


def read_server(cfg) -> dict:
    try:
        data = json.loads(server_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def refresh_server(cfg, run: Optional[Callable] = None, proc_root: str = "/proc") -> dict:
    """Ask tmux for its pid, map it to the Windows pid, and keep both in `server.json`. Returns
    the record in use: the fresh one, or the kept one when tmux did not answer."""
    msys_pid = ask_server_pid(cfg, run=run)
    if not msys_pid:
        return read_server(cfg)
    record = {"msys_pid": msys_pid, "winpid": winpid_of(msys_pid, proc_root),
              "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        path = server_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return record
