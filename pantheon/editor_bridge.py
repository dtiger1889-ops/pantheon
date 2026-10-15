"""Open a file or folder in VS Code on the desk.

The deck runs in Windows session 0 (sshd's), so a window program it starts never shows on
the user's monitor. The bridge: the deck writes the
exact VS Code command line to `state/editor/pending_open.json` and runs the Scheduled Task
`\\Pantheon\\OpenEditor`, which the user registers once. That task
runs in his desktop session and its script, `bin/open_editor.ps1`, runs the command.

`request_open` never raises and never waits for VS Code; it returns {"ok", "message"} with a
plain sentence either way.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

TASK_NAME = "\\Pantheon\\OpenEditor"
SETUP_DOC = "docs/editor-bridge-setup.md"
VSCODE_INSTALL = ("Programs", "Microsoft VS Code", "bin", "code.cmd")  # per-user installer layout

NO_CLI = ("VS Code's command line (code) isn't on this PC where Pantheon looks; "
          "set [editor] vscode_cli in pantheon.toml to its code.cmd")
NOT_SET_UP = f"VS Code bridge isn't set up (or you're not logged on at the desk) - see {SETUP_DOC}"
NO_PATH = "nothing to open: no file or folder is selected"


def to_posix(path: str) -> str:
    """`C:\\x\\y` -> `/c/x/y` under MSYS2 Python (os.name == "posix"); unchanged on Windows."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path or "")
    if os.name != "posix" or not m:
        return path
    return f"/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")


def to_windows(path: str) -> str:
    """`/c/x/y` (or `C:/x/y`) -> `C:\\x\\y`, the form VS Code needs. Other paths are unchanged."""
    m = re.match(r"^/([A-Za-z])(?:/(.*))?$", path or "")
    if m:
        return f"{m.group(1).upper()}:\\" + (m.group(2) or "").replace("/", "\\")
    if re.match(r"^[A-Za-z]:/", path or ""):
        return path.replace("/", "\\")
    return path


def find_code_cli(editor_cfg: dict, env: Optional[dict] = None,
                  which: Callable[[str], Optional[str]] = shutil.which,
                  exists: Callable[[str], bool] = os.path.exists) -> Optional[str]:
    """Where VS Code's `code.cmd` is, in Windows form, or None. `[editor] vscode_cli` wins; then
    the PATH; then the per-user install folder (the session's PATH under MSYS2 is stripped, so
    the PATH alone is not enough -- `computer_diagnosis/reference/CLI_TOOLS_PATH_INDEX.md`)."""
    env = os.environ if env is None else env
    candidates = []
    if editor_cfg.get("vscode_cli"):
        candidates.append(str(editor_cfg["vscode_cli"]))
    for name in ("code.cmd", "code"):
        found = which(name)
        if found:
            candidates.append(found)
    for base, tail in (("LOCALAPPDATA", ()), ("USERPROFILE", ("AppData", "Local"))):
        if env.get(base):
            candidates.append("\\".join((env[base].rstrip("\\/"),) + tail + VSCODE_INSTALL))
    for cand in candidates:
        if exists(to_posix(cand)):
            return to_windows(cand)
    return None


def build_command(code_cli: str, path: str, line: Optional[int] = None,
                  is_dir: bool = False) -> list[str]:
    """The VS Code command line: a folder opens as its own window; a file opens in the window
    already open (`--reuse-window`), at `line` when given."""
    target = to_windows(path)
    if is_dir:
        return [code_cli, target]
    if line:
        return [code_cli, "--reuse-window", "--goto", f"{target}:{int(line)}"]
    return [code_cli, "--reuse-window", target]


def schtasks_exe() -> str:
    for cand in ("/c/Windows/System32/schtasks.exe", "C:\\Windows\\System32\\schtasks.exe"):
        if os.path.exists(cand):
            return cand
    return "schtasks"


def request_open(editor_cfg: dict, state_dir: str, path: str, line: Optional[int] = None,
                 run: Callable = subprocess.run, env: Optional[dict] = None,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 exists: Callable[[str], bool] = os.path.exists) -> dict:
    """Ask the desk to open `path` (a file or a folder) in VS Code."""
    if not path:
        return {"ok": False, "message": NO_PATH}
    local = to_posix(path)
    if not os.path.exists(local):
        return {"ok": False, "message": f"can't open {path} in VS Code: it isn't there"}
    cli = find_code_cli(editor_cfg, env=env, which=which, exists=exists)
    if cli is None:
        return {"ok": False, "message": NO_CLI}
    argv = build_command(cli, path, line, is_dir=os.path.isdir(local))
    pending = Path(state_dir) / "editor" / "pending_open.json"
    try:
        pending.parent.mkdir(parents=True, exist_ok=True)
        tmp = pending.with_suffix(".tmp")
        tmp.write_text(json.dumps({"argv": argv, "requested": time.time()}), encoding="utf-8")
        os.replace(tmp, pending)
    except OSError as exc:
        return {"ok": False, "message": f"couldn't write {pending}: {exc}"}
    # MSYS2 would rewrite `/run` and `/tn` into folder paths; this turns that off for the call.
    child_env = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    try:
        proc = run([schtasks_exe(), "/run", "/tn", TASK_NAME], capture_output=True, text=True,
                   timeout=15, env=child_env)
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "message": NOT_SET_UP}
    if getattr(proc, "returncode", 1) != 0:
        return {"ok": False, "message": NOT_SET_UP}
    return {"ok": True, "message": f"opening {Path(local).name or path} in VS Code on the desk"}
