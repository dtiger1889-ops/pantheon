"""`python -m pantheon.hud.collector` -- the usage collector, a process of its own.

It owns `state/hud.json` while the budget window is open; the budget window only reads that file.
When the file is older than `sources.UNOWNED_AFTER_SECONDS` (this process is not running), the
deck's budget strip and the tmux status line run a file-only quick pass themselves
.

Why its own single-threaded process: the budget window used to run `sources.collect` on a
Textual thread worker. Under MSYS2 the fork that starts ccusage from that worker thread never came
back. The window's log went silent after 16:58 while a new stuck python child appeared under it every
60 seconds (twenty of them by 17:20, 38 MB each, one per refresh tick), until fork itself began
failing. A small experiment could not make a plain script do the same, so the exact trigger is not
pinned down; what is certain is that a single-threaded process has no other threads to fork around,
and that the window process no longer forks at all for usage.

What it does:
  - `sources.collect(cfg)` every REFRESH_INTERVAL_SECONDS, plus -- this process only -- the
    account usage read on its own slower schedule (`collect_pass`, `hud/account.py`);
  - a pass at once when the poke file (`state/hud.refresh-now`, written by the budget window's `r`
    key) appears, then removes the file;
  - stops when the tmux server it was started under is gone (`orphan.OrphanWatch`), or on SIGTERM.
`bin/pantheon` starts it in the background of the budget window's pane and ends it with the window.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from .. import config as config_mod
from .. import orphan
from . import sources

REFRESH_INTERVAL_SECONDS = 60
POKE_FILE = "hud.refresh-now"
SLEEP_STEP_SECONDS = 1.0     # how often the loop looks for the poke file and the tmux server

log = logging.getLogger("pantheon.hud.collector")


def poke_path(cfg) -> Path:
    return Path(cfg.hud_file).with_name(POKE_FILE)


def poke(cfg) -> None:
    """Ask the collector for fresh numbers now (the budget window's `r`). Never raises."""
    try:
        path = poke_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("now\n", encoding="utf-8")
    except OSError:
        pass


def _take_poke(cfg) -> bool:
    try:
        poke_path(cfg).unlink()
        return True
    except OSError:          # FileNotFoundError included: nobody asked
        return False


def parent_gone() -> bool:
    """True once the bash that started this collector is dead. `pantheon --restart` respawns the
    budget window with `respawn-window -k`, which kills that bash but not its backgrounded child;
    the collector was reparented to pid 1 and kept running -- three of them were writing the same
    hud.json and limits.jsonl on 2026-09-04 (started 16:26, 18:14, 21:58). The tmux-server watch
    never fires for this case because the server is alive."""
    try:
        return os.getppid() == 1
    except OSError:
        return False


def collect_pass(cfg):
    """One full pass, the only caller allowed to read the account's usage from the network
    (`hud/account.py` keeps its own five-minute schedule and back-off, so a 60-second tick or an
    `r` poke makes at most one request, and usually none)."""
    return sources.collect(cfg, poll_account=True)


def loop(cfg, collect: Callable = collect_pass, interval: float = REFRESH_INTERVAL_SECONDS,
         sleep: Callable[[float], None] = time.sleep, watch: Optional[orphan.OrphanWatch] = None,
         stop: Optional[Callable[[], bool]] = None, max_passes: Optional[int] = None,
         orphaned: Callable[[], bool] = parent_gone) -> int:
    """Run passes until told to stop; returns how many ran. The first pass runs at once. `collect`,
    `sleep`, `watch`, `stop`, `orphaned` and `max_passes` are injectable so the tests never wait
    or fork."""
    watch = watch if watch is not None else orphan.OrphanWatch()
    passes = 0
    due = 0.0                                    # monotonic time of the next scheduled pass
    while True:
        poked = _take_poke(cfg)
        if poked or time.monotonic() >= due:
            try:
                collect(cfg)
            except Exception:                    # a source misbehaving must not end the loop
                log.exception("usage pass failed")
            passes += 1
            due = time.monotonic() + interval
            if max_passes is not None and passes >= max_passes:
                return passes
        if stop is not None and stop():
            return passes
        if watch.active and watch.tick():
            return passes
        if orphaned():
            log.info("usage collector leaving: its window is gone")
            return passes
        sleep(SLEEP_STEP_SECONDS)


def main() -> int:
    cfg = config_mod.load()
    try:
        config_mod.ensure_state_dirs(cfg)
        logging.basicConfig(filename=str(cfg.log_file), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    except OSError:
        pass
    log.info("usage collector started (pid %s)", os.getpid())
    loop(cfg)
    return 0


if __name__ == "__main__":
    orphan.exit_hard(main())
