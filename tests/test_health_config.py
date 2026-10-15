"""`[health]` settings: every key and default from the spec, merged over by the
TOML, and documented in pantheon.example.toml."""
from __future__ import annotations

import dataclasses
import tomllib

from pantheon import config

SPEC_DEFAULTS = {
    "enabled": True, "probe_seconds": 30, "freeze_after_misses": 3, "capture_seconds": 60,
    "auto_restart": True, "storm_minutes": 60, "snapshot_seconds": 300, "sample_seconds": 60,
    "warn_handles": 10000, "low_commit_percent": 90, "planned_restart": True,
    "planned_restart_handles": 20000, "planned_idle_minutes": 10, "handle_exe": "",
    "procdump_exe": "", "command_log": True, "alerts": ["deck", "toast", "ntfy"],
    "close_empty_minutes": 5,
}


def test_defaults_match_the_spec():
    assert dataclasses.asdict(config.Config().health_settings()) == SPEC_DEFAULTS


def test_toml_merges_over_the_defaults(tmp_path):
    p = tmp_path / "pantheon.toml"
    p.write_text('[health]\nenabled = false\nwarn_handles = 500\nalerts = "toast"\n'
                 'command_log = "off"\nunknown = 1\n', encoding="utf-8")
    h = config.load(p).health_settings()
    assert h.enabled is False and h.warn_handles == 500 and h.alerts == ["toast"]
    assert h.command_log is False and h.probe_seconds == 30


def test_example_toml_documents_every_key_at_its_default():
    data = tomllib.loads((config.PROJECT_ROOT / "pantheon.example.toml").read_text(encoding="utf-8"))
    assert set(data["health"]) == set(SPEC_DEFAULTS)
    assert dataclasses.asdict(config.Health.from_dict(data["health"])) == SPEC_DEFAULTS


def test_a_throwaway_session_keeps_its_own_health_folder(tmp_path):
    """A test deck's watcher must never share the live one's pid file, server pid or status line."""
    from types import SimpleNamespace
    from pantheon import fullrestart
    from pantheon.health import sample, status
    live = SimpleNamespace(state_dir=str(tmp_path), tmux_session="pantheon")
    test = SimpleNamespace(state_dir=str(tmp_path), tmux_session="pantheon-test")
    assert sample.health_dir(live) == tmp_path / "health"
    assert sample.health_dir(test) == tmp_path / "health" / "pantheon-test"
    assert status.health_path(live) == tmp_path / "health.json"
    assert status.health_path(test) == tmp_path / "health" / "pantheon-test" / "health.json"
    assert fullrestart.last_health_snapshot_path(test).parent == tmp_path / "health" / "pantheon-test"
