"""The deck's health line (pantheon/health/status.py): only warn or bad, only
while the watcher keeps the file fresh, and the deck says it once per change."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pantheon import config
from pantheon.deck.app import DeckApp
from pantheon.health import status


def _cfg(tmp_path):
    return config.Config(state_dir=str(tmp_path / "state"))


def _write(cfg, level, message, age=0):
    when = datetime.now(timezone.utc) - timedelta(seconds=age)
    path = status.health_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"written_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "level": level, "message": message}))


def test_silent_when_healthy_missing_broken_or_stale(tmp_path):
    cfg = _cfg(tmp_path)
    assert status.health_message(cfg) == ("", "ok")
    _write(cfg, "ok", "")
    assert status.health_message(cfg) == ("", "ok")
    status.health_path(cfg).write_text("{broken")
    assert status.health_message(cfg) == ("", "ok")
    _write(cfg, "warn", "health: tmux server holds 11,000 handles", age=status.STALE_SECONDS + 5)
    assert status.health_message(cfg) == ("", "ok")


def test_warn_and_bad_are_shown(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, "warn", "health: tmux server holds 11,000 handles")
    assert status.health_message(cfg) == ("health: tmux server holds 11,000 handles", "warn")
    _write(cfg, "bad", "health: memory low (commit 95% of the limit)")
    assert status.health_message(cfg)[1] == "bad"


def test_the_deck_says_it_once_per_change(tmp_path):
    cfg = _cfg(tmp_path)
    said = []
    deck = SimpleNamespace(cfg=cfg, _say=lambda m, tone=None: said.append((m, tone)))
    DeckApp._check_health(deck)
    assert said == []
    _write(cfg, "warn", "health: tmux server holds 11,000 handles")
    DeckApp._check_health(deck)
    DeckApp._check_health(deck)
    assert said == [("health: tmux server holds 11,000 handles", "warning")]
    _write(cfg, "bad", "health: tmux server holds 25,000 handles")
    DeckApp._check_health(deck)
    assert said[-1] == ("health: tmux server holds 25,000 handles", "error")
