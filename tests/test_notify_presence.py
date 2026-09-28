"""Presence detection: the pure `is_present` rule, plus
the two I/O functions (`touch`/`read_deck_touch` on `state/presence`, `read_client_activity` off
a faked `tmuxctl.list_clients_with_activity`) that feed it in `runner.py`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pantheon import config as config_mod
from pantheon import tmuxctl
from pantheon.notify import presence as presence_mod

NOW = datetime(2026, 9, 2, 20, 0, 0, tzinfo=timezone.utc)


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def cfg_for(tmp_path):
    return config_mod.Config(state_dir=str(tmp_path / "state"))


# ---------------------------------------------------------------- is_present() (pure)


def test_not_present_when_no_client_attached():
    p = presence_mod.Presence(client_attached=False, deck_touched_at=_iso(NOW))
    assert presence_mod.is_present(p, 120, now=NOW) is False


def test_present_when_deck_touched_recently():
    p = presence_mod.Presence(client_attached=True, deck_touched_at=_iso(NOW - timedelta(seconds=30)))
    assert presence_mod.is_present(p, 120, now=NOW) is True


def test_present_when_client_activity_recent_even_without_a_deck_touch():
    p = presence_mod.Presence(client_attached=True, client_activity_at=_iso(NOW - timedelta(seconds=10)))
    assert presence_mod.is_present(p, 120, now=NOW) is True


def test_not_present_when_both_signals_are_stale():
    p = presence_mod.Presence(
        client_attached=True,
        deck_touched_at=_iso(NOW - timedelta(minutes=10)),
        client_activity_at=_iso(NOW - timedelta(minutes=10)),
    )
    assert presence_mod.is_present(p, 120, now=NOW) is False


def test_stale_presence_file_from_a_crashed_deck_does_not_claim_presence():
    """A client IS attached, but nothing about this
    row's own presence file or that client's activity is recent -- must not claim present."""
    p = presence_mod.Presence(client_attached=True, deck_touched_at=_iso(NOW - timedelta(hours=2)))
    assert presence_mod.is_present(p, 120, now=NOW) is False


# ---------------------------------------------------------------- touch() / read_deck_touch()


def test_touch_then_read_round_trips(tmp_path):
    cfg = cfg_for(tmp_path)
    presence_mod.touch(cfg)
    stamp = presence_mod.read_deck_touch(cfg)
    assert stamp is not None and stamp.endswith("Z")


def test_read_deck_touch_missing_file_is_none(tmp_path):
    cfg = cfg_for(tmp_path)
    assert presence_mod.read_deck_touch(cfg) is None


def test_touch_never_raises_when_the_directory_cannot_be_made(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)

    def boom(*a, **k):
        raise OSError("no")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    presence_mod.touch(cfg)  # must not raise


# ---------------------------------------------------------------- read_client_activity()


def test_read_client_activity_reports_attached_and_latest(monkeypatch):
    monkeypatch.setattr(tmuxctl, "list_clients_with_activity", lambda tmux=None: [
        ("/dev/pts/1", "pantheon", "1000"),
        ("/dev/pts/2", "pantheon", "2000"),
        ("/dev/pts/3", "other", "9999"),
    ])
    attached, activity_at = presence_mod.read_client_activity("pantheon")
    assert attached is True
    assert activity_at == datetime.fromtimestamp(2000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def test_read_client_activity_no_client_for_this_session(monkeypatch):
    monkeypatch.setattr(tmuxctl, "list_clients_with_activity", lambda tmux=None: [
        ("/dev/pts/1", "other", "1000"),
    ])
    attached, activity_at = presence_mod.read_client_activity("pantheon")
    assert attached is False and activity_at is None


# ---------------------------------------------------------------- current()


def test_current_combines_the_deck_touch_and_tmux_activity(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    presence_mod.touch(cfg)
    monkeypatch.setattr(tmuxctl, "list_clients_with_activity", lambda tmux=None: [
        ("/dev/pts/1", "pantheon", "1000"),
    ])
    snap = presence_mod.current(cfg)
    assert snap.client_attached is True
    assert snap.deck_touched_at is not None
    assert snap.client_activity_at is not None
