"""Restart Pantheon from inside Pantheon.

Until this, the only restart was `pantheon --restart` typed in a shell. `R` on the deck (and on
the queue, budget and standalone agents windows) now offers the same two restarts the launcher
has, each behind a confirm:

  - the deck, queue and budget windows only (`pantheon --restart`) -- agent windows keep running;
  - everything (`pantheon --restart --all`) -- asks twice, because it ends every agent window too.

Why the restart runs through `tmux run-shell -b` and not from this process: the launcher's
restart respawns window 0 with `respawn-window -k`, which kills the deck mid-call when the deck
itself is the caller. `run-shell -b` hands the launcher to the tmux SERVER, which starts it
detached from every pane, so the respawn finishes after the deck is gone. The launcher line ends
in `&` so `--restart --all` survives too: that one kills every Pantheon session, the tmux server
may exit with them, and a server that exits kills its own run-shell jobs -- a backgrounded child
the job shell already let go of is not one of them.

`--no-attach` goes on the end of both: a launcher started by the server has no terminal to
attach to, and its attach step would otherwise leave a stray grouped session behind (bin/pantheon,
the "Attach" block). Output goes to `state/restart.log`, the one place to look when a restart
did not happen.

The tmux call is made on the MAIN thread (the modal callbacks below run there): a fork from a
worker thread can hang under MSYS2 (see pantheon/orphan.py's history).
"""
from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path
from typing import Callable, Optional

from . import config as config_mod
from . import stage as stage_mod
from . import tmuxctl
from .widgets.modal import Confirm, Pick, PickOption

log = logging.getLogger("pantheon.restart")

# The words every restart screen uses, in one place so the deck and the standalone windows can
# never drift apart.
PICK_TITLE = "restart what?  (Esc = never mind)"
DECK_ONLY = "deck"
EVERYTHING = "all"
DECK_LABEL = "Restart deck, queue and budget"
DECK_DETAIL = "agents keep running"
ALL_LABEL = "Restart everything"
ALL_DETAIL = "asks twice"
CONFIRM_DECK = "restart the deck, queue and budget windows? your agents keep running"
CONFIRM_ALL_1 = "restart EVERYTHING? this closes every agent window too"
CONFIRM_ALL_1_BODY = "any agent mid-task stops where it is; you will need to open Pantheon again"
CONFIRM_ALL_2 = "sure? every agent window closes now"
# added to the first "everything" question only while an editor is open -- that restart is
# the one thing that ends it, so it must never do so silently.
CONFIRM_ALL_EDITOR = "; your open editor ({name}) closes too -- save it first"
STARTED_DECK = "restarting the deck, queue and budget windows -- agents keep running"
STARTED_ALL = "restarting everything -- open Pantheon again once the screen closes"
CANCELLED = "restart cancelled; nothing changed"
QUIT_NOTICE = "closed this window only; your agents keep running in their own windows"


def launcher_line(cfg: config_mod.Config, everything: bool = False,
                  root: Optional[Path] = None) -> str:
    """The shell line `run-shell -b` hands to the tmux server. `root` is the checkout this code
    was started from (the deck runs `python -m pantheon.deck` with its cwd set to it by
    bin/pantheon), so a restart always restarts the same copy of Pantheon that asked for it.
    `PANTHEON_SESSION` carries this deck's own session name across: bin/pantheon only reads its
    session name from that variable, never from pantheon.toml."""
    root = Path(root or config_mod.PROJECT_ROOT)
    launcher = (root / "bin" / "pantheon").as_posix()
    log_path = (Path(cfg.log_file).parent / "restart.log").as_posix()
    flags = "--restart --all" if everything else "--restart"
    env = f"PANTHEON_SESSION={shlex.quote(cfg.tmux_session)}"
    state_dir = os.environ.get("PANTHEON_STATE_DIR")
    if state_dir:
        env += f" PANTHEON_STATE_DIR={shlex.quote(state_dir)}"
    return (f"{env} {shlex.quote(launcher)} {flags} --no-attach "
            f">> {shlex.quote(log_path)} 2>&1 &")


def run_restart(cfg: config_mod.Config, everything: bool = False,
                run: Optional[Callable] = None) -> bool:
    """Hand the restart to the tmux server. True when tmux accepted the job (the restart itself
    happens after this returns -- and, for the deck, after this process is gone)."""
    # a session staged beside the deck lives in window 0, and the launcher's
    # `respawn-window -k` on window 0 would kill it -- put it back in its own window first, and
    # do not restart at all when tmux will not move it (bin/pantheon repeats this guard).
    # `release_all` also moves an open editor out to its own window, text and all.
    released = stage_mod.release_all(cfg, tmux=cfg.tools.tmux)
    if not released.get("ok"):
        log.warning("restart stopped: %s", released.get("message"))
        return False
    line = launcher_line(cfg, everything)
    log.info("restart requested (%s): %s", "everything" if everything else "deck windows", line)
    try:
        result = (run or tmuxctl.run)("run-shell", "-b", line, tmux=cfg.tools.tmux)
    except Exception:
        log.exception("restart could not reach tmux")
        return False
    ok = getattr(result, "returncode", 1) == 0
    if not ok:
        log.warning("tmux refused the restart: %r", getattr(result, "stderr", ""))
    return ok


def _editor_note(cfg: config_mod.Config) -> str:
    try:
        from . import editor as editor_mod

        found = editor_mod.where(cfg, tmux=cfg.tools.tmux)
    except Exception:
        return ""
    if not found:
        return ""
    name = Path(found.get("path") or "").name or "a file"
    return CONFIRM_ALL_EDITOR.format(name=name)


def ask(app, cfg: config_mod.Config, say: Callable[[str, Optional[str]], None],
        run: Optional[Callable] = None) -> None:
    """`R`: the two-choice pick, then the confirm(s), then the restart. Every step is a modal
    whose callback runs on the main thread, and nothing is fired while a modal is open (S-UI
    section 6.4). `say(message, tone)` is the window's own status line."""

    def failed() -> None:
        say(f"tmux did not take the restart; details are in {cfg.log_file}", "warning")

    def second_all(sure: Optional[bool]) -> None:
        if not sure:
            return say(CANCELLED, None)
        if run_restart(cfg, everything=True, run=run):
            say(STARTED_ALL, None)
        else:
            failed()

    def first_all(sure: Optional[bool]) -> None:
        if not sure:
            return say(CANCELLED, None)
        app.push_screen(Confirm(CONFIRM_ALL_2, yes_label="Yes, everything (y)", danger=True),
                        second_all)

    def deck_only(sure: Optional[bool]) -> None:
        if not sure:
            return say(CANCELLED, None)
        if run_restart(cfg, everything=False, run=run):
            say(STARTED_DECK, None)
        else:
            failed()

    def picked(choice: Optional[str]) -> None:
        if choice == DECK_ONLY:
            app.push_screen(Confirm(CONFIRM_DECK, yes_label="Yes, restart (y)"), deck_only)
        elif choice == EVERYTHING:
            app.push_screen(Confirm(CONFIRM_ALL_1, yes_label="Yes, everything (y)",
                                    body=CONFIRM_ALL_1_BODY + _editor_note(cfg), danger=True), first_all)
        else:
            say(CANCELLED, None)

    options = [PickOption("r", DECK_ONLY, DECK_LABEL, DECK_DETAIL),
               PickOption("a", EVERYTHING, ALL_LABEL, ALL_DETAIL)]
    app.push_screen(Pick(PICK_TITLE, options), picked)
