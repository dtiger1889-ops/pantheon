"""The editor pane: a terminal text editor opened as a
real tmux pane beside the SESSIONS list, in window 0, next to whatever session is on the stage.

Why a terminal editor in a tmux pane and not VS Code: every pane in the `pantheon` tmux is a
process sshd started, in Windows session 0, and a window program started from there never shows
on the user's monitor. A terminal editor just draws
wherever the tmux client is attached, desk or phone, with nothing to bridge.

Which editor: `[editor] command` in `pantheon.toml` when set; otherwise `micro` if installed, else
`nano`, else `$EDITOR`. None of them -> a plain sentence on screen, nothing opened.

The one rule this module keeps, same as `pantheon/stage.py`: a pane in window 0 is never killed
by a restart or a quit. `bin/pantheon --restart` respawns window 0 with `-k`, which ends every
pane in it -- so `park` moves the editor, text and all, out to its own `EDIT` window first
(`stage.release_all` calls it on every restart, quit and `b`), and `e` brings it back. Nothing
here ever kills or closes the editor: it goes away only when the user quits it, and then tmux closes
its pane by itself.

What is open lives in `state/editor.json` (`pane_id`, `path`). Every function returns what
actually happened and never raises.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config as config_mod
from . import stage as stage_mod
from . import tmuxctl

# The window a parked editor (and a phone-width one) lives in.
EDITOR_WINDOW = "EDIT"
# Tried in this order when `[editor] command` is not set.
DEFAULT_EDITORS = ("micro", "nano")
# Window programs cannot show from the tmux's Windows session (the module docstring) -- an
# `$EDITOR` naming one of these is passed over rather than started into nothing.
_WINDOW_PROGRAMS = {"notepad", "notepad.exe", "notepad++", "notepad++.exe", "code", "code.cmd",
                    "code.exe", "code-insiders", "subl", "subl.exe", "gvim", "gvim.exe", "wordpad"}
# Editors that take `+LINE file`, and the ones that take `file:LINE` instead.
_PLUS_LINE = {"micro", "nano", "nvim", "vim", "vi", "emacs", "mg", "kak"}
_COLON_LINE = {"hx", "helix"}

NOT_FOUND = ("no text editor found: micro and nano are not installed and $EDITOR is not set; "
             "install one (pacman -S micro) or set [editor] command in pantheon.toml")
CONFIGURED_MISSING = ("the editor set in pantheon.toml ([editor] command = \"{command}\") is not "
                      "installed; install it or change that line")


@dataclass(frozen=True)
class Editor:
    argv: list[str] = field(default_factory=list)   # the program (full path) plus its own flags
    name: str = ""                                   # the program's plain name: `nano`
    problem: str = ""                                # non-empty = nothing usable; say this instead

    @property
    def ok(self) -> bool:
        return bool(self.argv) and not self.problem


def _base(program: str) -> str:
    name = Path(program.replace("\\", "/")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def resolve(cfg: config_mod.Config, which: Callable[[str], Optional[str]] = shutil.which,
            env: Optional[dict] = None) -> Editor:
    """Which editor `e` starts. A command set in `pantheon.toml` is taken as meant -- if it is not
    installed that is said, not quietly swapped for another editor."""
    env = os.environ if env is None else env
    configured = str((cfg.editor or {}).get("command") or "").strip()
    if configured:
        try:
            parts = shlex.split(configured)
        except ValueError:
            parts = configured.split()
        found = which(parts[0]) if parts else None
        if not found:
            return Editor(problem=CONFIGURED_MISSING.format(command=configured))
        return Editor([found, *parts[1:]], _base(parts[0]))
    for name in DEFAULT_EDITORS:
        found = which(name)
        if found:
            return Editor([found], name)
    fallback = str(env.get("EDITOR") or "").strip()
    if fallback:
        try:
            parts = shlex.split(fallback)
        except ValueError:
            parts = fallback.split()
        if parts and _base(parts[0]) not in _WINDOW_PROGRAMS:
            found = which(parts[0])
            if found:
                return Editor([found, *parts[1:]], _base(parts[0]))
    return Editor(problem=NOT_FOUND)


def command_line(editor: Editor, path: str, line: Optional[int] = None) -> str:
    """The shell line tmux runs in the new pane. `line` is added only for editors known to take
    it (`+N file` for micro/nano/nvim/vim, `file:N` for helix); any other editor just opens the
    file."""
    path = _slashes(path)
    args = list(editor.argv)
    if line and line > 0 and editor.name in _PLUS_LINE:
        args += [f"+{line}", path]
    elif line and line > 0 and editor.name in _COLON_LINE:
        args.append(f"{path}:{line}")
    else:
        args.append(path)
    return shlex.join(args)


def _slashes(path: str) -> str:
    return str(path or "").replace("\\", "/")


# ------------------------------------------------------------------ the record


def _state_file(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / "editor.json"


def _read(cfg: config_mod.Config) -> Optional[dict]:
    try:
        data = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("pane_id"):
        return None
    return {"pane_id": str(data["pane_id"]), "path": str(data.get("path") or "")}


def _write(cfg: config_mod.Config, pane_id: str, path: str) -> None:
    try:
        target = _state_file(cfg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"pane_id": pane_id, "path": path}), encoding="utf-8")
    except OSError:
        pass


def _clear(cfg: config_mod.Config) -> None:
    try:
        _state_file(cfg).unlink()
    except OSError:
        pass


def _tmux(cfg: config_mod.Config, tmux: Optional[str]) -> Optional[str]:
    return tmux if tmux is not None else cfg.tools.tmux


def _ok(cp) -> bool:
    return getattr(cp, "returncode", 1) == 0


def _file_name(path: str) -> str:
    return Path(_slashes(path)).name or path


def where(cfg: config_mod.Config, tmux: Optional[str] = None) -> Optional[dict]:
    """The open editor, or None: `{"pane_id", "path", "window_index", "beside"}` where `beside`
    means it is in window 0 next to the SESSIONS list and `window_index` is None when tmux could
    not be asked. One tmux call, and only when a record exists. A pane tmux no longer has clears the record; a tmux that does not answer keeps it -- "unknown" is never
    "gone"."""
    record = _read(cfg)
    if record is None:
        return None
    cp = tmuxctl.run("list-panes", "-s", "-t", cfg.tmux_session, "-F", "#{pane_id}|#{window_index}",
                     tmux=_tmux(cfg, tmux))
    if not _ok(cp):
        return {**record, "window_index": None, "beside": False}
    for line in cp.stdout.splitlines():
        pane, _, index = line.partition("|")
        if pane == record["pane_id"]:
            try:
                window_index: Optional[int] = int(index)
            except ValueError:
                window_index = None
            return {**record, "window_index": window_index,
                    "beside": window_index == stage_mod.DECK_WINDOW}
    _clear(cfg)
    return None


def is_beside(cfg: config_mod.Config, tmux: Optional[str] = None) -> bool:
    found = where(cfg, tmux=tmux)
    return bool(found and found["beside"])


# ------------------------------------------------------------------ open / park


def _window_width(cfg: config_mod.Config, tmux: Optional[str]) -> Optional[int]:
    try:
        return int(tmuxctl.display("#{window_width}", tmux=tmux,
                                   target=f"{cfg.tmux_session}:{stage_mod.DECK_WINDOW}"))
    except ValueError:
        return None


def _deck_home(cfg: config_mod.Config) -> str:
    return f"{cfg.tmux_session}:{stage_mod.DECK_WINDOW}"


def rebalance(cfg: config_mod.Config, tmux: Optional[str] = None) -> None:
    """Sidebar at its own width, then the staged session and the editor sharing the rest evenly.
    Called after a session is staged while the editor is already open (joining the session in
    beside the narrow sidebar would otherwise leave it a sliver)."""
    tmux = _tmux(cfg, tmux)
    found = where(cfg, tmux=tmux)
    staged = stage_mod.current(cfg)
    if not (found and found["beside"]):
        return
    deck = stage_mod._deck_pane(cfg, tmux, exclude=staged.pane_id if staged else "")
    tmuxctl.run("resize-pane", "-t", deck, "-x", str(stage_mod.SIDEBAR_COLUMNS), tmux=tmux)
    width = _window_width(cfg, tmux)
    if staged is not None and width:
        share = max(20, (width - stage_mod.SIDEBAR_COLUMNS - 2) // 2)
        tmuxctl.run("resize-pane", "-t", staged.pane_id, "-x", str(share), tmux=tmux)


def open_beside_stage(cfg: config_mod.Config, path: str = "", line: Optional[int] = None,
                      cwd: str = "", tmux: Optional[str] = None,
                      min_width: int = stage_mod.MIN_STAGE_WIDTH,
                      which: Callable[[str], Optional[str]] = shutil.which,
                      env: Optional[dict] = None) -> dict:
    """Open `path` in the editor beside the SESSIONS list (right of the staged session, when one
    is on screen), and give it the keyboard.

    An editor that is already open is never opened twice: beside the list -> it just gets the
    keyboard; parked in its own window -> it is brought back beside the list. On a window too
    narrow for a sidebar and an editor side by side (the phone), the editor gets its own `EDIT`
    window and the viewer is switched to it instead -- the same rule the stage follows.

    Returns `{"ok": bool, "action": ..., "message": str, ...}`; `action` is `opened`, `focused`,
    `returned`, `window` (its own window, narrow screen), `switched` (to the parked one, narrow
    screen) or `none` (nothing happened; `message` says why)."""
    tmux = _tmux(cfg, tmux)
    session = cfg.tmux_session
    width = _window_width(cfg, tmux)
    wide = width is not None and width >= min_width

    found = where(cfg, tmux=tmux)
    if found is not None and found["window_index"] is not None:
        name = _file_name(found["path"])
        if found["beside"]:
            tmuxctl.run("select-pane", "-t", found["pane_id"], tmux=tmux)
            return {"ok": True, "action": "focused", "pane_id": found["pane_id"],
                    "message": f"the editor is already open beside the list ({name}); "
                               "quit it there to open a different file"}
        if not wide:
            ok = tmuxctl.select_window(session, found["window_index"], tmux=tmux)
            return {"ok": ok, "action": "switched" if ok else "none", "pane_id": found["pane_id"],
                    "message": f"switched to the editor's window ({name})" if ok
                    else "tmux would not switch to the editor's window"}
        staged = stage_mod.current(cfg)
        target = staged.pane_id if staged else stage_mod._deck_pane(cfg, tmux)
        if not _ok(tmuxctl.run("join-pane", "-h", "-s", found["pane_id"], "-t", target, tmux=tmux)):
            return {"ok": False, "action": "none", "pane_id": found["pane_id"],
                    "message": f"tmux would not bring the editor back; it is still in window "
                               f"{found['window_index']}"}
        rebalance(cfg, tmux=tmux)
        tmuxctl.run("select-pane", "-t", found["pane_id"], tmux=tmux)
        return {"ok": True, "action": "returned", "pane_id": found["pane_id"],
                "message": f"the editor is back beside the list ({name})"}

    if not path:
        return {"ok": False, "action": "none", "message": "no file was named, so nothing opened"}
    editor = resolve(cfg, which=which, env=env)
    if not editor.ok:
        return {"ok": False, "action": "none", "message": editor.problem}
    path = _slashes(path)
    folder = _slashes(cwd) or _slashes(str(Path(path).parent)) or cfg.projects_root
    shell = command_line(editor, path, line)
    name = _file_name(path)

    if not wide:
        cp = tmuxctl.run("new-window", "-t", session, "-n", EDITOR_WINDOW, "-c", folder,
                         "-P", "-F", "#{pane_id}", shell, tmux=tmux)
        if not _ok(cp) or not cp.stdout.strip():
            return {"ok": False, "action": "none",
                    "message": f"tmux would not open {editor.name}; nothing changed"}
        pane = cp.stdout.strip().splitlines()[0]
        _write(cfg, pane, path)
        return {"ok": True, "action": "window", "pane_id": pane,
                "message": f"{name} is open in {editor.name} in its own window ({EDITOR_WINDOW}); "
                           "F1 comes back to the deck"}

    staged = stage_mod.current(cfg)
    deck = stage_mod._deck_pane(cfg, tmux, exclude=staged.pane_id if staged else "")
    target = staged.pane_id if staged else deck
    cp = tmuxctl.run("split-window", "-h", "-t", target, "-c", folder, "-P", "-F", "#{pane_id}",
                     shell, tmux=tmux)
    if not _ok(cp) or not cp.stdout.strip():
        return {"ok": False, "action": "none",
                "message": f"tmux would not open {editor.name} beside the list; nothing changed"}
    pane = cp.stdout.strip().splitlines()[0]
    _write(cfg, pane, path)
    tmuxctl.run("resize-pane", "-t", deck, "-x", str(stage_mod.SIDEBAR_COLUMNS), tmux=tmux)
    tmuxctl.run("select-pane", "-t", pane, tmux=tmux)
    quit_hint = {"nano": " (Ctrl+S saves, Ctrl+X closes it)",
                 "micro": " (Ctrl+S saves, Ctrl+Q closes it)"}.get(editor.name, "")
    return {"ok": True, "action": "opened", "pane_id": pane,
            "message": f"{name} is open in {editor.name} beside the list{quit_hint}"}


def park(cfg: config_mod.Config, tmux: Optional[str] = None) -> dict:
    """Move an editor that is beside the list out to its own `EDIT` window, still running, text
    and all (`break-pane -d`: nobody's view moves). A no-op when no editor is beside the list.

    Returns `{"ok": bool, "action": "parked" | "none", "message": str}`; `ok` is False only when
    the editor is still in window 0 afterwards -- the caller must then not restart window 0."""
    tmux = _tmux(cfg, tmux)
    found = where(cfg, tmux=tmux)
    if found is None or not found["beside"]:
        return {"ok": True, "action": "none", "message": ""}
    cp = tmuxctl.run("break-pane", "-d", "-s", found["pane_id"], "-n", EDITOR_WINDOW,
                     "-P", "-F", "#{window_index}", tmux=tmux)
    if _ok(cp):
        index = (cp.stdout or "").strip()
        return {"ok": True, "action": "parked", "pane_id": found["pane_id"],
                "message": f"the editor moved to its own window, {EDITOR_WINDOW}"
                           + (f" ({index})" if index.isdigit() else "")
                           + ", with your text; e brings it back"}
    return {"ok": False, "action": "none", "pane_id": found["pane_id"],
            "message": "tmux would not move the editor out of the deck's window"}


def resolve_path(typed: str, folder: str) -> str:
    """A path typed into the `edit which file?` box: `~` expanded, relative to `folder`."""
    typed = _slashes(typed.strip().strip('"').strip("'"))
    if not typed:
        return ""
    if typed.startswith("~"):
        typed = _slashes(os.path.expanduser(typed))
    if typed.startswith("/") or (len(typed) > 1 and typed[1] == ":"):
        return typed
    return f"{_slashes(folder).rstrip('/')}/{typed}" if folder else typed
