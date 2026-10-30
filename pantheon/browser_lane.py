"""Browser lane: a browser on its own automation profile that SSH-side agents can drive.

`start` runs Chrome, Edge or Firefox on a dedicated profile folder (never the user's normal profile)
with a debugging port on 127.0.0.1 only, records the browser's Windows PID in state/browser/, and
prints the endpoint an agent attaches to. `stop` ends only that recorded PID, after checking it is
still the same process. `login <url>` is the one-time sign-in: on the desk it opens a normal window
with no debugging port; from session 0 it prints the one line to run on the desk instead, because a
window opened from session 0 is invisible (docs/browser-lane.md).
"""
from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CYGWIN = sys.platform in ("cygwin", "msys")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
START_TIMEOUT = 30.0
WINDOW_TIMEOUT = 15.0
# FILETIME counts 100 ns ticks from 1601; a process created this long before our launch is not ours.
CLOCK_SLACK = 2 * 10_000_000

BROWSERS = {
    "chrome": dict(kind="chromium", exes=("C:/Program Files/Google/Chrome/Application/chrome.exe",
                                          "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
                                          "%LOCALAPPDATA%/Google/Chrome/Application/chrome.exe")),
    "msedge": dict(kind="chromium", exes=("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
                                          "C:/Program Files/Microsoft/Edge/Application/msedge.exe")),
    "firefox": dict(kind="firefox", exes=("C:/Program Files/Mozilla Firefox/firefox.exe",
                                          "C:/Program Files (x86)/Mozilla Firefox/firefox.exe")),
}
# The everyday profiles for every browser; a lane profile may sit neither inside nor around one.
NORMAL_PROFILES = (
    "%LOCALAPPDATA%/Google/Chrome/User Data", "%LOCALAPPDATA%/Google/Chrome Beta/User Data",
    "%LOCALAPPDATA%/Google/Chrome SxS/User Data", "%LOCALAPPDATA%/Microsoft/Edge/User Data",
    "%LOCALAPPDATA%/Microsoft/Edge Beta/User Data", "%LOCALAPPDATA%/Microsoft/Edge Dev/User Data",
    "%LOCALAPPDATA%/Microsoft/Edge SxS/User Data", "%APPDATA%/Mozilla/Firefox",
    "%LOCALAPPDATA%/Mozilla/Firefox",
)
LOCKFILES = {"chromium": "lockfile", "firefox": "parent.lock"}


class LaneError(Exception):
    """A refusal or failure with a plain message for the caller."""


@dataclass
class ProcInfo:
    image: str      # full Windows path of the executable
    created: int    # FILETIME of creation


# ---------------------------------------------------------------- paths

def windows_env(env) -> dict:
    """LOCALAPPDATA / APPDATA even when an MSYS shell stripped them."""
    user = env.get("USERNAME") or getpass.getuser()
    drive = env.get("SystemDrive") or "C:"
    home = (env.get("USERPROFILE") or f"{drive}/Users/{user}").replace("\\", "/")
    return {"LOCALAPPDATA": (env.get("LOCALAPPDATA") or f"{home}/AppData/Local").replace("\\", "/"),
            "APPDATA": (env.get("APPDATA") or f"{home}/AppData/Roaming").replace("\\", "/")}


def expand(path: str, env) -> str:
    for key, value in windows_env(env).items():
        path = path.replace(f"%{key}%", value)
    return path.replace("\\", "/")


def mixed(path: str) -> str:
    """Windows path with forward slashes ("C:/x"); converts an MSYS path ("/c/x", "/tmp/x")."""
    path = str(path).replace("\\", "/")
    match = re.match(r"^/([a-zA-Z])(/|$)", path)
    if match:
        return f"{match.group(1).upper()}:/{path[3:]}".rstrip("/")
    if path.startswith("/") and CYGWIN:
        return subprocess.run(["cygpath", "-m", path], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, check=True).stdout.strip()
    return path.rstrip("/")


def native(path: str) -> str:
    """A Windows path in the form this Python opens: "/c/x" under MSYS2, "C:/x" elsewhere."""
    path = mixed(path)
    if CYGWIN and re.match(r"^[A-Za-z]:/", path):
        return f"/{path[0].lower()}{path[2:]}"
    return path


def backslash(path: str) -> str:
    return mixed(path).replace("/", "\\")


def _key(path: str) -> str:
    return mixed(path).lower().rstrip("/") + "/"


def inside(child: str, parent: str) -> bool:
    return _key(child).startswith(_key(parent))


def resolved(path: str) -> str:
    """`path` with symlinks and junctions followed, in Windows form; as given if unresolvable."""
    try:
        return mixed(os.path.realpath(native(path)))
    except (OSError, ValueError, subprocess.CalledProcessError):
        return path


# ---------------------------------------------------------------- Windows calls

class WinOps:
    """The few Windows calls the lane needs. Tests replace this with a fake."""

    def __init__(self):
        if os.name == "nt":
            self.k = ctypes.WinDLL("kernel32", use_last_error=True)
            self.u = ctypes.WinDLL("user32")
            proto = ctypes.WINFUNCTYPE
        else:  # MSYS2/Cygwin: same x64 calling convention
            self.k = ctypes.CDLL("kernel32.dll")
            self.u = ctypes.CDLL("user32.dll")
            proto = ctypes.CFUNCTYPE
        self.k.OpenProcess.restype = ctypes.c_void_p
        self.k.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        self.k.WTSGetActiveConsoleSessionId.restype = ctypes.c_ulong
        self._enum_proto = proto(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def session_id(self) -> int:
        out = ctypes.c_ulong()
        if not self.k.ProcessIdToSessionId(ctypes.c_ulong(self.k.GetCurrentProcessId()), ctypes.byref(out)):
            raise LaneError("could not read this process's Windows session")
        return out.value

    def console_session(self) -> int:
        return int(self.k.WTSGetActiveConsoleSessionId())

    def process_info(self, pid: int) -> Optional[ProcInfo]:
        handle = self.k.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        handle = ctypes.c_void_p(handle)
        try:
            code = ctypes.c_ulong()
            if not self.k.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None  # 259 = STILL_ACTIVE
            times = [ctypes.c_ulonglong() for _ in range(4)]
            if not self.k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if not self.k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return ProcInfo(image=buf.value, created=times[0].value)
        finally:
            self.k.CloseHandle(handle)

    def terminate(self, pid: int, created: Optional[int] = None) -> bool:
        """Stop `pid`. With `created`, only if the process behind the handle opened here has
        that creation time: the check and the kill share one handle, so a PID reused between
        an earlier `process_info` and this call is left alone."""
        # PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = self.k.OpenProcess(0x0001 | 0x100000 | 0x1000, 0, pid)
        if not handle:
            return False
        handle = ctypes.c_void_p(handle)
        try:
            if created is not None:
                times = [ctypes.c_ulonglong() for _ in range(4)]
                if (not self.k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times])
                        or times[0].value != created):
                    return False
            if not self.k.TerminateProcess(handle, ctypes.c_uint(1)):
                return False
            self.k.WaitForSingleObject(handle, ctypes.c_ulong(5000))
            return True
        finally:
            self.k.CloseHandle(handle)

    def visible_window_pids(self) -> set:
        pids = set()

        def visit(hwnd, _):
            if self.u.IsWindowVisible(ctypes.c_void_p(hwnd)):
                pid = ctypes.c_ulong()
                self.u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
                pids.add(pid.value)
            return 1

        self.u.EnumWindows(self._enum_proto(visit), None)
        return pids

    def listeners(self, port: int) -> list:
        """(local address, pid) for every TCP socket listening on `port`."""
        exe = "/c/Windows/System32/netstat.exe" if CYGWIN else r"C:\Windows\System32\netstat.exe"
        out = subprocess.run([exe, "-ano"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL).stdout
        return parse_netstat(out, port)

    def now(self) -> int:
        return int((time.time() + 11644473600) * 10_000_000)


def parse_netstat(text: str, port: int) -> list:
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            address, _, local_port = parts[1].rpartition(":")
            if local_port == str(port) and parts[4].isdigit():
                found.append((address, int(parts[4])))
    return found


def is_loopback(address: str) -> bool:
    address = address.strip("[]")
    return address.startswith("127.") or address == "::1"


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
                ("lpTitle", ctypes.c_wchar_p)] + [(f, ctypes.c_ulong) for f in (
                    "dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars",
                    "dwFillAttribute", "dwFlags")] + [
                ("wShowWindow", ctypes.c_ushort), ("cbReserved2", ctypes.c_ushort),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
                ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", ctypes.c_ulong), ("dwThreadId", ctypes.c_ulong)]


def _spawn(argv: list, cwd: str) -> int:
    """Start argv detached and return its Windows PID.

    CreateProcessW with handle inheritance off: a browser started through subprocess under MSYS2
    inherited the caller's output pipe, so an agent's shell call never returned while it ran
    (seen in a smoke test). No log file for the same reason: nothing of the caller's is passed on.
    """
    kernel = ctypes.WinDLL("kernel32") if os.name == "nt" else ctypes.CDLL("kernel32.dll")
    kernel.CreateProcessW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p,
                                      ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p)
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    info = _ProcessInformation()
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    if not kernel.CreateProcessW(argv[0], command, None, None, 0, flags, None, backslash(cwd),
                                 ctypes.byref(startup), ctypes.byref(info)):
        raise LaneError(f"Windows refused to start {argv[0]}")
    kernel.CloseHandle(ctypes.c_void_p(info.hThread))
    kernel.CloseHandle(ctypes.c_void_p(info.hProcess))
    return int(info.dwProcessId)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cdp_version(port: int) -> Optional[dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- the lane

class Lane:
    def __init__(self, state_dir: str, win=None, env=None, spawn: Callable = _spawn,
                 free_port: Callable = _free_port, cdp_version: Callable = _cdp_version,
                 sleep: Callable = time.sleep, monotonic: Callable = time.monotonic):
        self.state_dir = Path(state_dir) / "browser"
        self.win = win if win is not None else WinOps()
        self.env = dict(os.environ if env is None else env)
        self.spawn, self.free_port, self.cdp_version = spawn, free_port, cdp_version
        self.sleep, self.monotonic = sleep, monotonic

    # -- profile folder

    def profile_root(self) -> str:
        override = self.env.get("PANTHEON_BROWSER_PROFILES", "").strip()
        root = mixed(override) if override else expand("%LOCALAPPDATA%/pantheon/browser-profiles", self.env)
        return root.rstrip("/")

    def profile_dir(self, name: str) -> str:
        if not NAME_RE.match(name or ""):
            raise LaneError(f"profile name {name!r} must be lowercase letters, digits, - or _")
        path = f"{self.profile_root()}/{name}"
        self.check_profile_safe(path)
        return path

    def check_profile_safe(self, path: str) -> None:
        if not inside(path, self.profile_root()) or _key(path) == _key(self.profile_root()):
            raise LaneError(f"refusing profile {path}: it is not a folder under {self.profile_root()}")
        # Compared both as written and with links resolved: a junction under the lane root that
        # points into an everyday profile is that profile.
        mine = {path, resolved(path)}
        for normal in NORMAL_PROFILES:
            normal = expand(normal, self.env)
            for a in mine:
                for b in {normal, resolved(normal)}:
                    if inside(a, b) or inside(b, a):
                        raise LaneError(
                            f"refusing profile {path}: it overlaps the everyday profile {normal}")

    def profile_locked(self, kind: str, path: str) -> bool:
        """True while some browser holds the profile's lock file (it may be in another session)."""
        lock = os.path.join(native(path), LOCKFILES[kind])
        if not os.path.exists(lock):
            return False
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            return True
        os.close(fd)
        return False

    def find_exe(self, browser: str) -> str:
        for candidate in BROWSERS[browser]["exes"]:
            candidate = expand(candidate, self.env)
            if os.path.exists(native(candidate)):
                return candidate
        raise LaneError(f"{browser} is not installed in any of its usual folders")

    # -- state

    def state_file(self, name: str) -> Path:
        return self.state_dir / f"{name}.json"

    def read_state(self, name: str) -> Optional[dict]:
        try:
            return json.loads(self.state_file(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write_state(self, record: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.state_file(record["name"]).with_suffix(".tmp")
        temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(temp, self.state_file(record["name"]))

    def clear_state(self, name: str) -> None:
        try:
            self.state_file(name).unlink()
        except FileNotFoundError:
            pass

    def same_process(self, record: dict, info: Optional[ProcInfo]) -> bool:
        return bool(info and info.created == record.get("created")
                    and _key(info.image) == _key(record.get("image", "")))

    def live_record(self, name: str) -> Optional[dict]:
        record = self.read_state(name)
        if record and self.same_process(record, self.win.process_info(record["pid"])):
            return record
        return None

    # -- session

    def on_desktop(self) -> bool:
        session = self.win.session_id()
        return session != 0 and session == self.win.console_session()

    # -- commands

    def start(self, browser: str = "chrome", profile: Optional[str] = None, headed: bool = False,
              port: Optional[int] = None) -> dict:
        if browser not in BROWSERS:
            raise LaneError(f"unknown browser {browser!r}; use chrome, msedge or firefox")
        name = profile or browser
        kind = BROWSERS[browser]["kind"]
        if headed and not self.on_desktop():
            raise LaneError("a headed window started from session 0 would be invisible; "
                            "start headless here, or use `browser login <url>` for the desk")
        running = self.live_record(name)
        if running:
            return dict(running, note="already running")
        path = self.profile_dir(name)
        if self.profile_locked(kind, path):
            raise LaneError(f"profile {name} is open in another browser window (a sign-in window "
                            "still open on the desk?); close that window, then start again")
        exe = self.find_exe(browser)
        os.makedirs(native(path), exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if port is None:
            port = self.free_port()
        elif not 1024 <= port <= 65535 or self.win.listeners(port):
            raise LaneError(f"port {port} is out of range or already in use")
        argv = launch_args(kind, exe, path, port=port, headed=headed)
        launched_at = self.win.now()
        spawned = self.spawn(argv, path)
        deadline = self.monotonic() + START_TIMEOUT
        while self.monotonic() < deadline:
            sockets = self.win.listeners(port)
            if sockets:
                return self._adopt(sockets, name, browser, kind, exe, path, port, headed, launched_at)
            self.sleep(0.5)
        spawned_info = self.win.process_info(spawned)
        if self._ours(spawned_info, exe, launched_at):
            self.win.terminate(spawned, created=spawned_info.created)
        raise LaneError(f"{browser} did not open its debugging port within {START_TIMEOUT:.0f}s; "
                        "stopped the process it started")

    def _adopt(self, sockets, name, browser, kind, exe, path, port, headed, launched_at) -> dict:
        pids = {pid for _, pid in sockets}
        mine = {pid: self.win.process_info(pid) for pid in pids}
        if len(pids) != 1 or not all(self._ours(info, exe, launched_at) for info in mine.values()):
            raise LaneError(f"port {port} was taken by a process this lane did not start; nothing stopped")
        pid, info = next(iter(mine.items()))
        exposed = [address for address, _ in sockets if not is_loopback(address)]
        if exposed:
            self.win.terminate(pid, created=info.created)
            raise LaneError(f"{browser} listened on {', '.join(exposed)}, not only 127.0.0.1; stopped it")
        if kind == "chromium":
            deadline = self.monotonic() + START_TIMEOUT
            while not self.cdp_version(port):
                if self.monotonic() >= deadline:
                    self.win.terminate(pid, created=info.created)
                    raise LaneError(f"{browser} never answered /json/version on port {port}; stopped it")
                self.sleep(0.5)
            endpoint = f"http://127.0.0.1:{port}"
        else:
            endpoint = f"ws://127.0.0.1:{port}/session"
        record = dict(name=name, browser=browser, kind=kind, pid=pid, created=info.created,
                      image=info.image, port=port, endpoint=endpoint, profile_dir=path,
                      headless=not headed, session_id=self.win.session_id(),
                      listening=sorted({address for address, _ in sockets}),
                      started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        self.write_state(record)
        return record

    def _ours(self, info: Optional[ProcInfo], exe: str, launched_at: int) -> bool:
        return bool(info and os.path.basename(mixed(info.image)).lower() == os.path.basename(exe).lower()
                    and info.created >= launched_at - CLOCK_SLACK)

    def status(self, name: Optional[str] = None) -> list:
        names = [name] if name else sorted(p.stem for p in self.state_dir.glob("*.json"))
        rows = []
        for each in names:
            record = self.read_state(each)
            if not record:
                rows.append(dict(name=each, running=False))
                continue
            live = self.same_process(record, self.win.process_info(record["pid"]))
            row = dict(record, running=live)
            if live:
                sockets = self.win.listeners(record["port"])
                row["listening"] = sorted({address for address, _ in sockets})
                row["loopback_only"] = bool(sockets) and all(is_loopback(a) for a, _ in sockets)
            rows.append(row)
        return rows

    def stop(self, name: str) -> str:
        record = self.read_state(name)
        if not record:
            return f"{name}: no lane browser recorded; nothing stopped"
        info = self.win.process_info(record["pid"])
        if info is None:
            self.clear_state(name)
            return f"{name}: was not running (PID {record['pid']} gone); record cleared"
        if not self.same_process(record, info):
            self.clear_state(name)
            return (f"{name}: PID {record['pid']} now belongs to another process ({info.image}); "
                    "left it alone and cleared the record")
        if not self.win.terminate(record["pid"], created=record.get("created")):
            raise LaneError(f"{name}: could not stop PID {record['pid']}")
        self.clear_state(name)
        return f"{name}: stopped PID {record['pid']}"

    def login(self, url: str, browser: str = "chrome", profile: Optional[str] = None) -> dict:
        if not re.match(r"^https?://[^\s'\"]+$", url or ""):
            raise LaneError("the sign-in address must be a plain http:// or https:// URL")
        if browser not in BROWSERS:
            raise LaneError(f"unknown browser {browser!r}; use chrome, msedge or firefox")
        name = profile or browser
        kind = BROWSERS[browser]["kind"]
        path = self.profile_dir(name)
        if self.live_record(name):
            raise LaneError(f"the lane browser for profile {name} is running; run `browser stop"
                            f"{'' if name == browser else ' --profile ' + name}` first, "
                            "sign-in needs the profile to itself")
        exe = self.find_exe(browser)
        argv = launch_args(kind, exe, path, port=None, headed=True, url=url)
        if not self.on_desktop():
            return dict(mode="desk_line", line=powershell_line(argv), profile_dir=path)
        if self.profile_locked(kind, path):
            raise LaneError(f"a window on profile {name} is already open; finish signing in there")
        os.makedirs(native(path), exist_ok=True)
        launched_at = self.win.now()
        self.spawn(argv, path)
        deadline = self.monotonic() + WINDOW_TIMEOUT
        while self.monotonic() < deadline:
            for pid in self.win.visible_window_pids():
                if self._ours(self.win.process_info(pid), exe, launched_at):
                    return dict(mode="opened", pid=pid, profile_dir=path)
            self.sleep(0.5)
        return dict(mode="no_window", profile_dir=path)


def launch_args(kind: str, exe: str, profile: str, port: Optional[int], headed: bool,
                url: str = "about:blank") -> list:
    """Command line for one lane browser. No port = sign-in window (nothing listening)."""
    argv = [backslash(exe)]
    if kind == "chromium":
        argv += [f"--user-data-dir={backslash(profile)}", "--no-first-run", "--no-default-browser-check"]
        if port is not None:
            argv += [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"]
        if not headed:
            argv.append("--headless=new")
    else:
        argv += ["--profile", backslash(profile), "--no-remote"]
        if port is not None:
            argv += ["--remote-debugging-port", str(port)]
        if not headed:
            argv.append("--headless")
    argv.append(url)
    return argv


def powershell_line(argv: list) -> str:
    """One line the user can paste into PowerShell on the desk (single quotes: nothing expands)."""
    return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in argv)


# ---------------------------------------------------------------- CLI

def _describe(row: dict) -> str:
    if not row.get("running"):
        return f"{row['name']}: not running"
    mode = "headless" if row.get("headless") else "headed"
    guard = "" if row.get("loopback_only", True) else "  WARNING: not loopback-only"
    return (f"{row['name']}: {row['browser']} {mode}, PID {row['pid']}, session {row['session_id']}, "
            f"endpoint {row['endpoint']}{guard}")


def main(argv=None, lane: Optional[Lane] = None) -> int:
    parser = argparse.ArgumentParser(prog="browser", description="Pantheon browser lane")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start", help="start the lane browser and print its endpoint")
    p_start.add_argument("--browser", default="chrome", choices=sorted(BROWSERS))
    p_start.add_argument("--profile")
    p_start.add_argument("--headed", action="store_true", help="visible window (desk sessions only)")
    p_start.add_argument("--port", type=int, help="fixed port (for an MCP config); default: any free one")
    p_status = sub.add_parser("status", help="show lane browsers")
    p_status.add_argument("--profile")
    p_status.add_argument("--json", action="store_true")
    p_stop = sub.add_parser("stop", help="stop the recorded lane browser")
    p_stop.add_argument("--profile", default="chrome")
    p_login = sub.add_parser("login", help="one-time sign-in window on the desk")
    p_login.add_argument("url")
    p_login.add_argument("--browser", default="chrome", choices=sorted(BROWSERS))
    p_login.add_argument("--profile")
    args = parser.parse_args(argv)
    if lane is None:
        from pantheon import config as config_mod
        lane = Lane(config_mod.load().state_dir)
    try:
        if args.cmd == "start":
            record = lane.start(args.browser, args.profile, args.headed, args.port)
            print(record["endpoint"])
            print(_describe(dict(record, running=True)) + (" (already running)" if record.get("note") else ""),
                  file=sys.stderr)
        elif args.cmd == "status":
            rows = lane.status(args.profile)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print("\n".join(_describe(row) for row in rows) or "no lane browsers recorded")
        elif args.cmd == "stop":
            print(lane.stop(args.profile))
        elif args.cmd == "login":
            result = lane.login(args.url, args.browser, args.profile)
            if result["mode"] == "desk_line":
                print("This session is on the SSH side, where a browser window would be invisible.")
                print("Run this one line in PowerShell on the desk, sign in, then close the window:")
                print(result["line"])
            elif result["mode"] == "opened":
                print(f"Sign-in window open (PID {result['pid']}). Sign in, then close the window; "
                      "agents use `browser start` afterwards.")
            else:
                print("Started the sign-in browser but saw no window on the desk within "
                      f"{WINDOW_TIMEOUT:.0f}s; check the taskbar.")
                return 1
    except LaneError as exc:
        print(f"browser: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
