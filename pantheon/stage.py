"""The stage: an agent's REAL terminal beside
the deck's sidebar, Orca-style.

Clicking a running session moves that agent's tmux pane into window 0, to the right of the deck
pane (`join-pane -h`), shrinks the deck pane to sidebar width and gives the agent pane the
keyboard, so typing goes straight into Claude. Staging another session first breaks the current
one back out into its own window under its original name. Same technique as the pit
(`pantheon/pit/tmux_pit.py`): panes are moved, never copied, never killed.

What is staged lives in `state/stage.json` (`pane_id`, `window_name`, `session_id`) so the
separate `python -m pantheon.stage release` run (bin/pantheon's `--restart`, which respawns
window 0 with `-k` and would otherwise kill a staged agent) knows what to put back.

Every function here returns what actually happened and never raises: a tmux call that fails
leaves things where they were and says so.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import config as config_mod
from . import tmuxctl
from .models import TmuxWindow

DECK_WINDOW = 0
# The deck pane's width while something is staged: the sidebar plus its border.
SIDEBAR_COLUMNS = 38
# Narrower than this and there is no room for a sidebar AND a usable terminal side by side (the
# phone): a click switches to the agent's own window instead.
MIN_STAGE_WIDTH = 120

# OFF since 2026-09-27: the first live join of a pane running a native Windows program
# (`claude.exe`, the Assistant) wedged the whole tmux server at 01:14:00 -- 0% CPU, every thread
# waiting, every client hung; earlier joins of MSYS2 programs (bash, nano) were fine. Until the
# cause is found, a click jumps to the session's own window (the narrow-window path below).
# Tests turn it back on (`tests/conftest.py`) so the join code stays covered.
JOIN_PANES = False

# `python -m pantheon.stage release` exits with this when a staged agent
# is still in window 0 after trying to move it out -- bin/pantheon's restart stops rather than
# kill it.
EXIT_STILL_STAGED = 3


@dataclass(frozen=True)
class Staged:
    pane_id: str          # the agent's tmux pane, e.g. `%7`
    window_name: str      # the name its own window had, given back on release
    session_id: str = ""  # the agent session it belongs to, when the caller knew it


def _state_file(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / "stage.json"


def _read(cfg: config_mod.Config) -> Optional[Staged]:
    try:
        data = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("pane_id"):
        return None
    return Staged(str(data["pane_id"]), str(data.get("window_name") or data["pane_id"]),
                  str(data.get("session_id") or ""))


def _write(cfg: config_mod.Config, staged: Staged) -> None:
    try:
        path = _state_file(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(staged)), encoding="utf-8")
    except OSError:
        pass


def _clear(cfg: config_mod.Config) -> None:
    try:
        _state_file(cfg).unlink()
    except OSError:
        pass


def _home(cfg: config_mod.Config) -> str:
    return f"{cfg.tmux_session}:{DECK_WINDOW}"


def _tmux(cfg: config_mod.Config, tmux: Optional[str]) -> Optional[str]:
    return tmux if tmux is not None else cfg.tools.tmux


def _ok(cp) -> bool:
    return getattr(cp, "returncode", 1) == 0


def current(cfg: config_mod.Config, verify: bool = False,
            tmux: Optional[str] = None) -> Optional[Staged]:
    """What is staged right now, or None. `verify=True` also asks tmux (one call) whether that
    pane is still in window 0, and clears the record when it is not -- the agent exited, or
    something moved it. When tmux cannot be asked at all, the record is kept: "unknown" is never
    treated as "gone"."""
    staged = _read(cfg)
    if staged is None or not verify:
        return staged
    panes = tmuxctl.list_panes(_home(cfg), tmux=_tmux(cfg, tmux))
    if panes and staged.pane_id not in panes:
        _clear(cfg)
        return None
    return staged


def _deck_pane(cfg: config_mod.Config, tmux: Optional[str], exclude: str = "") -> str:
    """The deck's own pane in window 0: this process's `$TMUX_PANE` when it lives there (the deck
    calling), else the first pane of window 0 that is not the staged one (the CLI calling)."""
    panes = tmuxctl.list_panes(_home(cfg), tmux=tmux)
    mine = os.environ.get("TMUX_PANE", "")
    if mine and mine in panes:
        return mine
    for pane in panes:
        if pane != exclude:
            return pane
    return _home(cfg)


def _active_pane(target: str, tmux: Optional[str]) -> Optional[tuple[str, str]]:
    """`(pane_id, window_name)` of the active pane in window `target`, or None when that window
    does not exist. Asked with `list-panes`, which FAILS on a missing window -- not
    `display-message`, which silently falls back to whatever pane is current."""
    cp = tmuxctl.run("list-panes", "-t", target, "-F", "#{pane_active}|#{pane_id}|#{window_name}", tmux=tmux)
    if not _ok(cp):
        return None
    rows = [line.split("|", 2) for line in cp.stdout.splitlines() if line.count("|") >= 2]
    for active, pane, name in rows:
        if active == "1":
            return pane, name or pane
    return (rows[0][1], rows[0][2] or rows[0][1]) if rows else None


def _window_width(cfg: config_mod.Config, tmux: Optional[str]) -> Optional[int]:
    try:
        return int(tmuxctl.display("#{window_width}", tmux=tmux, target=_home(cfg)))
    except ValueError:
        return None


def stage(cfg: config_mod.Config, window_index: int, session_id: str = "",
          tmux: Optional[str] = None, min_width: int = MIN_STAGE_WIDTH) -> dict:
    """Put the agent in window `window_index` beside the deck.

    Returns `{"ok": bool, "action": ..., "message": str, ...}` where `action` is `staged`,
    `focused` (it was already staged; the keyboard goes back to it), `switched` (window 0 is too
    narrow, so the viewer was moved to the agent's own window instead) or `none` (nothing
    happened; `message` says why)."""
    tmux = _tmux(cfg, tmux)
    session = cfg.tmux_session
    if window_index == DECK_WINDOW:
        # Only a staged agent reports window 0 (the deck's own pane is never an agent row).
        staged = current(cfg)
        if staged is not None:
            tmuxctl.run("select-pane", "-t", staged.pane_id, tmux=tmux)
            return {"ok": True, "action": "focused", "pane_id": staged.pane_id,
                    "window_name": staged.window_name, "message": f"{staged.window_name} is on screen"}
        return {"ok": False, "action": "none", "message": "that is the deck's own window"}

    target = f"{session}:{window_index}"
    width = _window_width(cfg, tmux)
    if not JOIN_PANES or width is None or width < min_width:
        ok = tmuxctl.select_window(session, window_index, tmux=tmux)
        return {"ok": ok, "action": "switched" if ok else "none",
                "message": f"switched to window {window_index}" if ok
                else f"tmux would not switch to window {window_index}"}

    found = _active_pane(target, tmux)
    if found is None:
        return {"ok": False, "action": "none", "message": f"window {window_index} is not there any more"}
    agent_pane, name = found

    staged = current(cfg, verify=True, tmux=tmux)
    if agent_pane in tmuxctl.list_panes(_home(cfg), tmux=tmux) and (
            staged is None or staged.pane_id != agent_pane):
        # Never move the deck's own pane (or anything else already in window 0) anywhere.
        return {"ok": False, "action": "none", "message": "that pane is already in the deck's window"}
    if staged is not None and staged.pane_id == agent_pane:
        tmuxctl.run("select-pane", "-t", agent_pane, tmux=tmux)
        return {"ok": True, "action": "focused", "pane_id": agent_pane, "window_name": staged.window_name,
                "message": f"{staged.window_name} is on screen"}
    if staged is not None:
        released = release(cfg, tmux=tmux)
        if not released["ok"]:
            return {"ok": False, "action": "none", "message": released["message"]}

    deck = _deck_pane(cfg, tmux)
    if not _ok(tmuxctl.run("join-pane", "-h", "-s", agent_pane, "-t", deck, tmux=tmux)):
        return {"ok": False, "action": "none",
                "message": f"tmux would not move {name} beside the deck; it is still in window {window_index}"}
    _write(cfg, Staged(agent_pane, name, session_id or ""))
    tmuxctl.run("resize-pane", "-t", deck, "-x", str(SIDEBAR_COLUMNS), tmux=tmux)
    tmuxctl.run("select-pane", "-t", agent_pane, tmux=tmux)
    return {"ok": True, "action": "staged", "pane_id": agent_pane, "window_name": name,
            "message": f"{name} is on screen; type to it, or click the sidebar to come back"}


def focus_deck(cfg: config_mod.Config, tmux: Optional[str] = None) -> bool:
    """Give the keyboard back to the deck's own pane (the staged agent stays on screen)."""
    tmux = _tmux(cfg, tmux)
    staged = current(cfg)
    return _ok(tmuxctl.run("select-pane", "-t", _deck_pane(cfg, tmux, exclude=staged.pane_id if staged else ""),
                           tmux=tmux))


def release(cfg: config_mod.Config, tmux: Optional[str] = None) -> dict:
    """Put a staged agent back in its own window, under its old name, without switching anyone's
    view to it (`break-pane -d`). The deck pane gets the whole window back on its own (tmux gives
    a closed-off pane's space to its neighbour). A no-op when nothing is staged.

    Returns `{"ok": bool, "action": "released" | "gone" | "none", "message": str, ...}`; `ok` is
    False only when the agent is still in window 0 afterwards."""
    tmux = _tmux(cfg, tmux)
    staged = _read(cfg)
    if staged is None:
        return {"ok": True, "action": "none", "message": "nothing was on screen beside the deck"}
    panes = tmuxctl.list_panes(_home(cfg), tmux=tmux)
    if panes and staged.pane_id not in panes:
        _clear(cfg)
        return {"ok": True, "action": "gone", "message": f"{staged.window_name} had already closed"}
    cp = tmuxctl.run("break-pane", "-d", "-s", staged.pane_id, "-n", staged.window_name,
                     "-P", "-F", "#{window_index}", tmux=tmux)
    if _ok(cp):
        _clear(cfg)
        try:
            index: Optional[int] = int(cp.stdout.strip())
        except (ValueError, AttributeError):
            index = None
        return {"ok": True, "action": "released", "window_index": index, "window_name": staged.window_name,
                "message": f"{staged.window_name} is back in its own window"
                           + (f" ({index})" if index is not None else "")}
    if not panes:
        # tmux could not even list window 0: it is not there to kill anything either.
        return {"ok": True, "action": "none", "message": "tmux is not answering; nothing was moved"}
    return {"ok": False, "action": "none",
            "message": f"tmux would not move {staged.window_name} back to its own window"}


def release_all(cfg: config_mod.Config, tmux: Optional[str] = None) -> dict:
    """Clear window 0 of everything beside the deck, without ending any of it: the staged session
    goes back to its own window (`release`) and an open editor moves to its own `EDIT` window with
    its text. What `b`, quit and both restarts call, because
    `respawn-window -k` on window 0 ends every pane in it.

    Same return shape as `release`; `ok` is False when either one is still in window 0."""
    from . import editor as editor_mod   # editor imports this module; import here, not at the top

    first = release(cfg, tmux=tmux)
    try:
        second = editor_mod.park(cfg, tmux=tmux)
    except Exception as exc:   # an unreadable record must never block a restart of the rest
        second = {"ok": True, "action": "none", "message": "", "error": repr(exc)}
    acted = [r for r in (first, second) if r.get("action") not in (None, "none")]
    failed = [r for r in (first, second) if not r.get("ok")]
    shown = failed or acted or [first]
    return {**first, "ok": not failed,
            "action": "released" if acted else first.get("action", "none"),
            "editor": second,
            "message": "; ".join(r["message"] for r in shown if r.get("message"))}


def with_staged(windows: list[TmuxWindow], cfg: config_mod.Config,
                tmux: Optional[str] = None) -> list[TmuxWindow]:
    """`windows` plus the staged agent's pane, reported as window 0. `list-windows` only reports a
    window's ACTIVE pane, so once the keyboard is back on the sidebar the staged agent would drop
    out of every list; this puts it back. No tmux call at all when nothing is staged."""
    staged = _read(cfg)
    if staged is None or any(w.pane_id == staged.pane_id for w in windows):
        return windows
    cp = tmuxctl.run("list-panes", "-t", _home(cfg), "-F", tmuxctl._FMT, tmux=_tmux(cfg, tmux))
    if not _ok(cp):
        return windows
    extra: list[TmuxWindow] = []
    for line in cp.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 6 or parts[4] != staged.pane_id:
            continue
        try:
            extra.append(TmuxWindow(int(parts[0]), parts[1], parts[2], parts[3], parts[4], parts[5]))
        except ValueError:
            continue
    return list(windows) + extra


def _cli_cfg() -> config_mod.Config:
    import dataclasses

    cfg = config_mod.load()
    session = os.environ.get("PANTHEON_SESSION")
    return dataclasses.replace(cfg, tmux_session=session) if session else cfg


def main(argv: Optional[list[str]] = None) -> int:
    """`python -m pantheon.stage release` or `python -m pantheon.stage status`."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "status"
    try:
        cfg = _cli_cfg()
    except Exception as exc:     # a broken pantheon.toml must not block a restart
        print(f"stage: could not read the settings ({exc}); nothing was moved")
        return 0
    if command == "release":
        result = release_all(cfg)
        if result["action"] != "none" or not result["ok"]:
            print(f"stage: {result['message']}")
        return 0 if result["ok"] else EXIT_STILL_STAGED
    if command == "status":
        staged = current(cfg, verify=True)
        print(f"stage: {staged.window_name} ({staged.pane_id}) is beside the deck" if staged
              else "stage: nothing is beside the deck")
        return 0
    print("usage: python -m pantheon.stage [release|status]")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
