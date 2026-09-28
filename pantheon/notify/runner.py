"""The 2-second notify loop. A standalone `bin/notifier`
process, its own tmux window beside the governor -- same reason governor is standalone
(`governor/runner.py`'s docstring): the user closes the deck, his agents keep running, and this
loop must keep watching them regardless. All memory lives in files (`store.py`), so a crash or a
restart picks up exactly where it left off (acceptance 6).

Design note on "tail from the last offset" (the spec's wording for reading `events.jsonl`): this
runner re-reads the WHOLE file every tick, the same choice `governor/runner.py` already made and
ships with. `supervisor.state.fold` needs every event for a session to derive its current
status correctly -- handing it only the lines written since the last tick would silently drop
every session with nothing new to say this tick, which is most of them most ticks. True
incremental tailing would only save the re-parse cost; `rules.decide`'s dedupe (`history`,
persisted in `store.py`) is what actually guarantees nothing fires twice, so the full re-read is
a straightforward correctness choice, not a shortcut. Perf sweep, 2026-09-02: that re-parse cost
now goes through `eventcache.read_events_cached`, the same cache the deck's panes and the
governor runner use -- still a full read whenever the file has actually changed, just not a
second full read of the same unchanged bytes within the same size+mtime window.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .. import config as config_mod
from .. import eventcache as eventcache_mod
from .. import tmuxctl
from ..governor import parked as governor_parked
from ..hud import sources as hud_sources
from ..supervisor import state as state_mod
from . import channels as channels_mod
from . import presence as presence_mod
from . import rules
from . import store

log = logging.getLogger("pantheon.notify.runner")

TICK_SECONDS = 2


def _status_of(row) -> str:
    return row.status.value if hasattr(row.status, "value") else str(row.status)


def _iso(dt: datetime) -> str:
    """Format an INJECTED `now`, never `models.utcnow_iso()`'s real wall clock -- every timestamp
    this runner writes (`sent.jsonl`, `history[key]["last_notified_at"]`) must agree with the
    clock `decide()`'s nudge-window math is using, or a test (or a real clock skew) makes a fresh
    notification look minutes old the instant it is written."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class NotifyRunner:
    """One tick's worth of notify work. `tmux`/`sleep`/`now_fn` and the three senders are all
    injectable -- the same shape `governor.runner.GovernorRunner` uses, so the whole thing runs
    against fakes in tests, no real tmux, no real network, no real clock."""

    cfg: object
    tmux: Optional[str] = None
    sleep: Callable[[float], None] = time.sleep
    now_fn: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    send_toast: Callable = channels_mod.toast
    send_ntfy: Callable = channels_mod.ntfy
    send_telegram: Callable = channels_mod.telegram

    _prev_status: dict = field(default_factory=dict)
    _history: dict = field(default_factory=dict)
    _prev_hud: dict = field(default_factory=dict)
    _loaded_memory: bool = False

    # ------------------------------------------------------------ settings + memory

    def _settings(self):
        return self.cfg.notify_settings()

    def _load_memory_once(self) -> None:
        if self._loaded_memory:
            return
        self._prev_status, self._history = store.read_memory(self.cfg)
        self._loaded_memory = True

    # ------------------------------------------------------------ reads

    def _events(self) -> list:
        # perf sweep item 1: shared with the deck's panes and the governor runner via the same
        # (size, mtime) cache (`eventcache.py`) -- still a full re-read whenever the file has
        # actually changed since the last reader anywhere asked, per this module's own docstring.
        events, _errors = eventcache_mod.read_events_cached(self.cfg.events_file)
        return events

    def _windows(self) -> list:
        return tmuxctl.list_windows(self.cfg.tmux_session, tmux=self.tmux)

    def _rows(self, events, windows, now):
        mtimes = hud_sources.statusline_mtimes(self.cfg.statusline_dir)
        return state_mod.fold(events, windows, now, self.cfg.tmux_session, self.cfg.projects_root,
                              statusline_mtimes=mtimes)

    def _park_refusals(self) -> list[dict]:
        return [
            line for line in governor_parked._read_lines(governor_parked.parked_path(self.cfg))
            if line.get("kind") == "park_refused"
        ]

    # ------------------------------------------------------------ the tick

    def tick(self) -> None:
        settings = self._settings()
        if not settings.enabled:
            return
        self._load_memory_once()

        now = self.now_fn()
        events = self._events()
        windows = self._windows()
        rows = self._rows(events, windows, now)
        cur_hud = hud_sources.read_hud(self.cfg)
        presence = presence_mod.current(self.cfg, tmux=self.tmux)

        edges = rules.transitions(self._prev_status, rows, now=now)
        edge_ids = {t.session_id for t in edges}
        still_needs_you = [
            rules.transition_for(row, self._prev_status, now)
            for row in rows
            if row.session_id not in edge_ids and _status_of(row) in rules.NEEDS_YOU_STATUSES
        ]

        notifications: list[rules.Notification] = []
        for t in edges + still_needs_you:
            n = rules.decide(t, presence, settings, self._history, now=now)
            if n is not None:
                notifications.append(n)
        notifications += rules.budget_events(self._prev_hud, cur_hud, self._history)
        notifications += rules.park_refused_notifications(self._park_refusals(), self._history)
        notifications += rules.handoff_notifications(
            [e for e in events if (e.event or "").lower() == "handoff"], self._history
        )

        for n in notifications:
            self._fire(n, settings, now)

        self._prev_status = {row.session_id: _status_of(row) for row in rows if row.session_id}
        self._prev_hud = cur_hud
        store.write_memory(self.cfg, self._prev_status, self._history)

    # ------------------------------------------------------------ firing one notification

    def _fire(self, n: rules.Notification, settings, now: datetime) -> None:
        allowed = rules.allowed_during_quiet_hours(n.tier, settings.quiet_hours, now)
        record: dict = {
            "ts": _iso(now), "key": n.key, "channels": list(n.channels),
            "title": n.title, "body": n.body, "detail": n.detail, "tier": n.tier,
            "dry_run": bool(settings.dry_run),
        }

        if settings.dry_run:
            record["ok"] = True
        elif not allowed:
            record["ok"] = False
            record["suppressed"] = "quiet"
        else:
            results = self._send(n, settings)
            record["results"] = [{"channel": ch, "ok": ok, "detail": detail} for ch, ok, detail in results]
            record["ok"] = bool(results) and all(ok for _ch, ok, _d in results)
            for ch, ok, detail in results:
                if not ok:
                    log.warning("notify: %s send failed for %s: %s", ch, n.key, detail)

        store.append_sent(self.cfg, record)
        self._remember(n, now)

    def _send(self, n: rules.Notification, settings) -> list[tuple[str, bool, str]]:
        out: list[tuple[str, bool, str]] = []
        for channel in n.channels:
            if channel == "toast":
                if not settings.toast_enabled:
                    continue
                out.append(("toast", *self.send_toast(n.title, n.body, self.cfg.tools.pwsh)))
            elif channel == "ntfy":
                if not settings.ntfy_enabled:
                    continue
                priority = "high" if n.tier == rules.TIER_WARNING else "default"
                out.append(("ntfy", *self.send_ntfy(settings.ntfy_url, settings.ntfy_topic, n.title, n.body, priority)))
            elif channel == "telegram":
                if not settings.telegram_enabled:
                    continue
                text = f"{n.title}\n{n.body}"
                out.append(("telegram", *self.send_telegram(
                    text, settings.telegram_chat_id, settings.telegram_credential_target, self.cfg.tools.pwsh,
                )))
        return out

    def _remember(self, n: rules.Notification, now: datetime) -> None:
        """Update `history[n.key]` every time a `Notification` is DECIDED, whether it actually
        reached a channel or was dry-run/quiet-suppressed -- this is what keeps the one-shot
        branches from re-firing every tick and the nudge ladder on schedule regardless of quiet
        hours (see `store.py`'s and `rules._once`'s docstrings)."""
        entry = dict(self._history.get(n.key) or {})
        if n.title == "Still waiting":
            entry["nudges_sent"] = int(entry.get("nudges_sent", 0)) + 1
        else:
            entry.setdefault("nudges_sent", 0)
            entry.setdefault("first_notified_at", _iso(now))
        entry["last_notified_at"] = _iso(now)
        self._history[n.key] = entry


def main(argv: Optional[list[str]] = None) -> int:
    cfg = config_mod.load()
    config_mod.ensure_state_dirs(cfg)
    settings = cfg.notify_settings()
    logging.basicConfig(
        filename=str(cfg.log_file), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    print(f"pantheon notifier: enabled={settings.enabled} dry_run={settings.dry_run}")
    if not settings.enabled:
        print("notify is disabled in pantheon.toml ([notify] enabled = false); nothing to do")
        print("this window stays open so the user can flip it and restart without hunting for the log")
    # The fixed production loop: 2 seconds forever. Tests exercise `NotifyRunner.tick()`
    # directly (see tests/test_notify_runner.py) rather than this loop.
    runner = NotifyRunner(cfg=cfg)
    while True:
        try:
            runner.tick()
        except Exception:  # a bad tick must not kill the loop -- the deck's own rows stay the first signal either way
            log.exception("notify tick failed")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
