"""The 60-second governor loop. A standalone process (`bin/governor`,
its own tmux window `gov`), never a thread inside the deck -- the user closes the deck, his agents
keep running, and a governor that died with the deck would strand them. All of its memory lives
in files (`parked.py`), so a crash or a reboot picks up exactly where it was.

Every tmux call goes through `tmuxctl`, which already sends literal text and Enter as two
separate `send-keys` calls. Every
decision is a plain call into `policy.py`; this module only does the file reads, the tmux calls,
and holds the loop together.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import config as config_mod
from .. import events as events_mod
from .. import eventcache as eventcache_mod
from .. import tmuxctl
from ..hud import sources as hud_sources
from ..models import AgentState, Event, TmuxWindow, format_clock, parse_ts, utcnow_iso
from ..supervisor import state as state_mod
from . import parked as parked_mod
from . import policy

log = logging.getLogger("pantheon.governor.runner")

TICK_SECONDS = 60
HUD_STALE_SECONDS = 120
RESUME_WAIT_SECONDS = 60.0
RESUME_COMMANDS = {"node", "node.exe", "claude", "claude.exe"}

WINDOW_WORDS = {"five_hour": "five-hour", "seven_day": "weekly"}


def _reset_clock(resets_at: Optional[str], clock: str = "24h") -> str:
    return format_clock(parse_ts(resets_at), clock)


def wind_down_text(window: str, percent: float, resets_at: Optional[str], clock: str = "24h") -> str:
    """The one line typed into a session about to hit its limit."""
    return (
        f"Usage is at {percent:.0f}% of the {WINDOW_WORDS.get(window, window)} window, "
        f"which resets at {_reset_clock(resets_at, clock)}. Run /checkpoint now, then stop. "
        "Do not start new work."
    )


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class GovernorRunner:
    """One tick's worth of governor work. `tmux`/`sleep`/`now_fn` are injectable so the whole
    thing runs against a fake tmux and a fixed clock in tests -- the same shape `providers/claude.py`
    uses for the same reason."""

    cfg: object
    tmux: Optional[str] = None
    sleep: Callable[[float], None] = time.sleep
    now_fn: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    _acted: dict = field(default_factory=dict)  # window -> resets_at last wound down for
    _rungs: dict = field(default_factory=dict)  # session_id -> last resume rung tried
    _refused: set = field(default_factory=set)  # session ids already told "max_parked"

    # ------------------------------------------------------------ settings

    def _gov(self):
        return self.cfg.governor_settings()

    # ------------------------------------------------------------ logging (real vs dry-run)

    def _log_event(self, event: str, session_id: Optional[str] = None, **extra) -> None:
        known = {"session_id", "cwd", "project", "tmux_pane", "job_id", "message", "detail", "tool_name"}
        fields = {k: v for k, v in extra.items() if k in known}
        rest = {k: v for k, v in extra.items() if k not in known}
        if self._gov().dry_run:
            record = {"ts": utcnow_iso(), "event": f"would_{event}", "session_id": session_id, **fields, **rest}
            parked_mod.append_dryrun(self.cfg, record)
            return
        try:
            events_mod.append_event(self.cfg.events_file, Event(
                ts=utcnow_iso(), event=event, source="pantheon", session_id=session_id,
                extra=rest, **fields,
            ))
        except OSError:
            pass

    def _type(self, target: str, text: str) -> None:
        """Types into a session's prompt. Dry run never types anything."""
        if self._gov().dry_run:
            return
        # Only ever type into a pane that is running Claude Code right now:
        # a row can still carry a window index for a beat after the agent exited and a shell
        # took the pane over, and a wind-down sentence typed into bash is a stray command.
        running = tmuxctl.pane_command(target, self.tmux).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if running not in RESUME_COMMANDS:
            log.info("governor: not typing into %s (pane runs %r, not Claude Code)", target, running)
            return
        tmuxctl.send_text(target, text, tmux=self.tmux)

    # ------------------------------------------------------------ reads

    def _picture(self) -> dict:
        picture = hud_sources.read_hud(self.cfg)
        if policy.hud_is_stale(picture.get("fetched_at"), self.now_fn(), HUD_STALE_SECONDS):
            try:
                picture = hud_sources.collect(self.cfg, write=False)
            except Exception:  # a source misbehaving must not stop the loop
                log.exception("governor: could not read usage sources directly")
        return picture

    def _events(self) -> list[Event]:
        # perf sweep item 1: shared with the deck's panes and the notify runner via the same
        # (size, mtime) cache -- see `eventcache.py`'s docstring.
        events, _errors = eventcache_mod.read_events_cached(self.cfg.events_file)
        return events

    def _windows(self) -> list[TmuxWindow]:
        return tmuxctl.list_windows(self.cfg.tmux_session, tmux=self.tmux)

    def _agent_rows(self, events: list[Event], windows: list[TmuxWindow]) -> list[AgentState]:
        mtimes = hud_sources.statusline_mtimes(self.cfg.statusline_dir)
        return state_mod.fold(events, windows, self.now_fn(), self.cfg.tmux_session,
                              self.cfg.projects_root, statusline_mtimes=mtimes)

    # ------------------------------------------------------------ the tick

    def tick(self) -> None:
        gov = self._gov()
        if not gov.enabled:
            return
        now = self.now_fn()
        events = self._events()
        windows = self._windows()
        picture = self._picture()

        self._wind_down_pass(picture.get("claude"), events, windows, now, gov)
        self._park_pass(events, now, gov)
        self._resume_pass(windows, events, now, gov)

    # ------------------------------------------------------------ 1. wind-down (section 5)

    def _wind_down_pass(self, claude_usage, events, windows, now, gov) -> None:
        crossings = policy.check_windows(claude_usage, gov.wind_down_at_percent, self._acted)
        if not crossings:
            return
        rows = self._agent_rows(events, windows)
        targets = [r for r in policy.agents_to_wind_down(rows) if r.window_index is not None]
        clock = self.cfg.appearance_settings().clock
        for crossing in crossings:
            text = wind_down_text(crossing.window, crossing.percent, crossing.resets_at, clock)
            for row in targets:
                target = f"{row.tmux_session or self.cfg.tmux_session}:{row.window_index}"
                self._type(target, text)
                self._log_event(
                    "wind_down", session_id=row.session_id, cwd=row.cwd, project=row.project,
                    tmux_pane=row.tmux_pane, window=crossing.window, percent=crossing.percent,
                    resets_at=crossing.resets_at,
                )
                if not gov.dry_run:
                    parked_mod.set_pending(self.cfg, row.session_id, {
                        "project": row.project, "cwd": row.cwd, "window_name": row.window_name,
                        "parked_at_deadline": _iso(now.timestamp() + gov.grace_seconds),
                        "resume_after": crossing.resets_at,
                    })
            self._acted[crossing.window] = crossing.resets_at or ""

    # ------------------------------------------------------------ 2. parking (section 6)

    def _park_pass(self, events, now, gov) -> None:
        if gov.dry_run:
            return  # dry run never writes parked.jsonl (section 8 point 4)
        pending = parked_mod.read_pending(self.cfg)
        if not pending:
            return
        ended = {e.session_id for e in events if (e.event or "").lower() == "sessionend" and e.session_id}
        already = set(parked_mod.open_parks(self.cfg))
        decisions = policy.parking_decisions(pending, ended, now, already, gov.max_parked)
        for d in decisions:
            entry = pending.get(d.session_id) or {}
            if d.should_park:
                parked_mod.append_park(self.cfg, parked_mod.Parked(
                    session_id=d.session_id, project=entry.get("project"), cwd=entry.get("cwd"),
                    window_name=entry.get("window_name"), parked_at=utcnow_iso(),
                    reason=d.reason, resume_after=entry.get("resume_after") or "",
                ))
                self._log_event("park", session_id=d.session_id, cwd=entry.get("cwd"), project=entry.get("project"))
                parked_mod.clear_pending(self.cfg, d.session_id)
                self._refused.discard(d.session_id)
            elif d.session_id not in self._refused:
                parked_mod.append_park_refused(self.cfg, d.session_id, len(already), gov.max_parked)
                self._refused.add(d.session_id)

    # ------------------------------------------------------------ 3. resume (section 7)

    def _resume_pass(self, windows, events, now, gov) -> None:
        if gov.dry_run or not gov.auto_resume:
            return
        open_parks = parked_mod.open_parks(self.cfg)
        for session_id in policy.resume_order(list(open_parks.values())):
            record = open_parks[session_id]
            if not policy.ready_to_resume(record, now):
                continue
            by = policy.already_resumed(session_id, record.get("cwd"), windows, events)
            if by:
                parked_mod.append_resumed(self.cfg, session_id, by=by)
                self._log_event("resume", session_id=session_id, by=by, rung=0)
                continue
            self._attempt_resume(session_id, record, gov)

    def _attempt_resume(self, session_id: str, record: dict, gov) -> None:
        cwd = record.get("cwd")
        window_name = (record.get("window_name") or "resume")[:20]
        rung = policy.next_rung(self._rungs.get(session_id, 0))
        self._rungs[session_id] = rung
        session, tmux = self.cfg.tmux_session, self.tmux
        claude = self.cfg.tools.claude

        if rung == 4:
            self._log_event("resume", session_id=session_id, rung=4, cwd=cwd, project=record.get("project"))
            return  # Caution amber, top of sort (models.AgentStatus.RESUME_FAILED); the user's call now

        command = {1: f"{claude} --resume {session_id}", 2: f"{claude} -c", 3: claude}[rung]
        index = tmuxctl.new_window(session, window_name, cwd or ".", tmux=tmux, detached=True)
        if index is None:
            self._log_event("resume", session_id=session_id, rung=rung, cwd=cwd, ok=False)
            return
        target = f"{session}:{index}"
        if cwd:
            tmuxctl.send_text(target, f'cd "{cwd}"', tmux=tmux)
        tmuxctl.send_text(target, command, tmux=tmux)
        ready = tmuxctl.wait_for_command(target, RESUME_COMMANDS, RESUME_WAIT_SECONDS, tmux=tmux, sleep=self.sleep)
        prompt = gov.resume_prompt
        if rung == 3:
            prompt = f"Read CHECKPOINT.md to the end first, then: {prompt}"
        if ready:
            tmuxctl.send_text(target, prompt, tmux=tmux)
        self._log_event(
            "resume", session_id=session_id, rung=rung, cwd=cwd, project=record.get("project"),
            window_index=index, ok=bool(ready),
        )
        if ready:
            parked_mod.append_resumed(self.cfg, session_id, by=f"governor-rung-{rung}")


def main(argv: Optional[list[str]] = None) -> int:
    cfg = config_mod.load()
    config_mod.ensure_state_dirs(cfg)
    gov = cfg.governor_settings()
    logging.basicConfig(
        filename=str(cfg.log_file), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    print(f"pantheon governor: enabled={gov.enabled} dry_run={gov.dry_run}")
    if not gov.enabled:
        print("governor is disabled in pantheon.toml ([governor] enabled = false); nothing to do")
        print("this window stays open so the user can flip it and restart without hunting for the log")
    # The fixed production loop: 60 seconds forever. Tests exercise `GovernorRunner.tick()`
    # directly (see tests/test_governor_runner.py) rather than this loop.
    runner = GovernorRunner(cfg=cfg)
    while True:
        try:
            runner.tick()
        except Exception:  # a bad tick must not kill the loop -- the user's agents keep running either way
            log.exception("governor tick failed")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
