"""Restart Pantheon from inside Pantheon.

Until this, the only restart was `pantheon --restart` typed in a shell. `R` on the deck (and on
the queue, budget and standalone agents windows) now offers the same two restarts the launcher
has, each behind a confirm:

  - everything (`pantheon --restart --all`), the default since one confirm, then a wait screen naming every Claude session still
    mid-turn (Esc cancels, nothing is ended), then the launcher's full restart, which reopens each
    Claude session on its own conversation (`pantheon/fullrestart.py`);
  - the deck, queue and budget windows only (`pantheon --restart`) -- agent windows keep running.

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

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from . import config as config_mod
from . import fullrestart as full_mod
from . import stage as stage_mod
from . import tmuxctl
from .widgets.modal import Confirm, Pick, PickOption

log = logging.getLogger("pantheon.restart")

# The words every restart screen uses, in one place so the deck and the standalone windows can
# never drift apart.
PICK_TITLE = "restart what?  (Esc = never mind)"
DECK_ONLY = "deck"
EVERYTHING = "all"
DECK_LABEL = "Restart deck only"
DECK_DETAIL = "deck, queue, budget; agents keep running"
ALL_LABEL = "Restart everything"
ALL_DETAIL = "Pantheon + Claude; sessions come back"
CONFIRM_DECK = "restart the deck, queue and budget windows? your agents keep running"
CONFIRM_ALL_1 = "restart everything? each Claude session comes back on its own conversation"
CONFIRM_ALL_1_BODY = ("waits until every session is between turns; open shells and other "
                      "windows close; running Codex jobs keep running")
# added to the "everything" question only while an editor is open -- that restart is the one
# thing that ends it, so it must never do so silently.
CONFIRM_ALL_EDITOR = "; your open editor ({name}) closes too -- save it first"
WAIT_TITLE = "waiting for these sessions to finish their turn"
WAIT_HINT = "Esc cancels -- nothing has been ended yet"
STARTED_DECK = "restarting the deck, queue and budget windows -- agents keep running"
STARTED_ALL = ("restarting everything -- each Claude session reopens in its own window; "
               "open Pantheon again once the screen closes")
CANCELLED = "restart cancelled; nothing changed"
NOT_ALL = "not restarting everything, nothing was ended: {why}"
QUIT_NOTICE = "closed this window only; your agents keep running in their own windows"
# `q` asks first: one plain question, `Yes` in the ordinary colour
# because closing a window ends no agent. `n`, Esc or a second `q` leave everything as it was.
CONFIRM_QUIT = "close this window? your agents keep running in their own windows"
QUIT_YES = "Yes, close it (y)"
QUIT_KEPT = "kept this window open"


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
    # `--ready`: the deck's wait screen already waited, so the launcher checks once and stops
    # (fail closed) if a session started a turn since, instead of waiting where nobody sees it.
    flags = "--restart --all --ready" if everything else "--restart"
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


class RestartWait(ModalScreen[Optional[bool]]):
    """names every Claude session a full restart is still waiting on, re-checked every
    `fullrestart.POLL_SECONDS`; dismisses with True the moment none is mid-turn, None on Esc.
    The one screen here that closes on its own -- it is a progress screen, not a question."""

    BINDINGS = [("escape", "dismiss(None)", "cancel")]
    DEFAULT_CSS = """
    RestartWait { align: center middle; }
    RestartWait > Vertical#box {
        width: 60; height: auto; max-height: 20;
        border: round $secondary; background: $panel; padding: 1 2;
    }
    RestartWait #title { width: 100%; padding-bottom: 1; }
    RestartWait #hint { width: 100%; color: $text-muted; padding-top: 1; }
    """

    def __init__(self, busy: Callable[[], list]) -> None:
        super().__init__()
        self._busy = busy
        self._done = False

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(WAIT_TITLE, id="title")
            yield Static("", id="who")
            yield Label(WAIT_HINT, id="hint")

    def on_mount(self) -> None:
        self.check()
        self.set_interval(full_mod.POLL_SECONDS, self.check)

    def check(self) -> None:
        if self._done:
            return
        try:
            names = self._busy()
        except Exception:
            log.exception("could not read the sessions while waiting")
            names = ["(could not read the sessions; see state/pantheon.log)"]
        if not names:
            self._done = True
            self.dismiss(True)
            return
        self.query_one("#who", Static).update("\n".join(f"  {n}" for n in names))


def _full_rows(cfg: config_mod.Config) -> list:
    return full_mod.current_rows(cfg)


def full_restart_problems(cfg: config_mod.Config, rows_source: Optional[Callable] = None) -> list:
    """Fail closed before the confirm: a Claude window with no known conversation id."""
    try:
        rows = (rows_source or _full_rows)(cfg)
    except Exception as exc:
        return [f"could not read the sessions ({exc})"]
    return full_mod.build_snapshot(cfg, rows)[1]


def full_restart_busy(cfg: config_mod.Config, rows_source: Optional[Callable] = None) -> list:
    rows = (rows_source or _full_rows)(cfg)
    return [full_mod.label(r) for r in full_mod.waiting_on(rows, cfg.tmux_session)]


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


def ask_quit(app, close: Callable[[], None],
             say: Optional[Callable[[str, Optional[str]], None]] = None) -> None:
    """`q` on the deck, queue, budget and agents windows: ask, then `close()` only on Yes. A `q`
    pressed while any question is already open does nothing, so a double tap never stacks two
    questions or closes the window behind one."""
    if isinstance(app.screen, ModalScreen):
        return

    def answered(sure: Optional[bool]) -> None:
        if sure:
            close()
        elif say is not None:
            say(QUIT_KEPT, None)

    app.push_screen(Confirm(CONFIRM_QUIT, yes_label=QUIT_YES, no_label="No (n)"), answered)


def ask(app, cfg: config_mod.Config, say: Callable[[str, Optional[str]], None],
        run: Optional[Callable] = None, rows_source: Optional[Callable] = None) -> None:
    """`R`: the two-choice pick, then the confirm(s), then the restart. Every step is a modal
    whose callback runs on the main thread, and nothing is fired while a modal is open (S-UI
    section 6.4). `say(message, tone)` is the window's own status line."""

    def failed() -> None:
        say(f"tmux did not take the restart; details are in {cfg.log_file}", "warning")

    def waited(ready: Optional[bool]) -> None:
        if not ready:
            return say(CANCELLED, None)
        if run_restart(cfg, everything=True, run=run):
            say(STARTED_ALL, None)
        else:
            failed()

    def first_all(sure: Optional[bool]) -> None:
        if not sure:
            return say(CANCELLED, None)
        app.push_screen(RestartWait(lambda: full_restart_busy(cfg, rows_source)), waited)

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
            problems = full_restart_problems(cfg, rows_source)
            if problems:
                return say(NOT_ALL.format(why="; ".join(problems)), "warning")
            app.push_screen(Confirm(CONFIRM_ALL_1, yes_label="Yes, everything (y)",
                                    body=CONFIRM_ALL_1_BODY + _editor_note(cfg), danger=True), first_all)
        else:
            say(CANCELLED, None)

    # "Restart everything" first, so Enter picks it; `a` and `r` keep their old meanings.
    options = [PickOption("a", EVERYTHING, ALL_LABEL, ALL_DETAIL),
               PickOption("r", DECK_ONLY, DECK_LABEL, DECK_DETAIL)]
    app.push_screen(Pick(PICK_TITLE, options), picked)
