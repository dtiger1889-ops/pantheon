from pathlib import Path

import pytest

from pantheon import config


def test_defaults_when_missing(tmp_path):
    cfg = config.load(tmp_path / "missing.toml")
    assert cfg.tmux_session == "pantheon"
    assert cfg.enabled_providers() == ["claude", "codex"]
    assert cfg.events_file.name == "events.jsonl"


def test_load_the_shipped_example_toml():
    cfg = config.load(config.PROJECT_ROOT / "pantheon.example.toml")
    assert cfg.vault.startswith("C:/") and "~" not in cfg.vault
    assert cfg.sprints_dir == Path(cfg.vault) / "Projects" / "Sprints"
    assert cfg.governor.get("dry_run") is True


def test_partial_toml_merges(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('refresh_seconds = 9\n[providers]\nollama = true\n[tools]\ntmux = "/x/tmux"\n', encoding="utf-8")
    cfg = config.load(p)
    assert cfg.refresh_seconds == 9
    assert cfg.providers == {"claude": True, "codex": True, "ollama": True}
    assert cfg.tools.tmux == "/x/tmux"
    assert "nodejs" in cfg.node_env()["PATH"].split(";")[0] + cfg.node_env()["PATH"].split(":")[0]


# ---------------------------------------------------------------- refresh_seconds


def test_refresh_seconds_zero_is_a_valid_manual_only_setting():
    assert config.Config(refresh_seconds=0).refresh_seconds == 0


def test_refresh_seconds_below_zero_is_rejected():
    with pytest.raises(ValueError):
        config.Config(refresh_seconds=-1)


# ---------------------------------------------------------------- governor settings


def test_governor_settings_default_when_the_toml_has_none():
    gov = config.Config().governor_settings()
    assert gov.enabled is False and gov.dry_run is True
    assert gov.grace_seconds == 300 and gov.max_parked == 6 and gov.auto_resume is True
    assert gov.wind_down_at_percent == {"five_hour": 85.0, "seven_day": 90.0}
    assert "CHECKPOINT.md" in gov.resume_prompt


def test_governor_settings_merges_the_toml_over_the_defaults():
    cfg = config.Config(governor={"enabled": True, "max_parked": 3,
                                  "wind_down_at_percent": {"five_hour": 80}})
    gov = cfg.governor_settings()
    assert gov.enabled is True and gov.max_parked == 3
    assert gov.dry_run is True  # untouched keys keep their default
    assert gov.wind_down_at_percent == {"five_hour": 80, "seven_day": 90.0}  # merged, not replaced


def test_real_toml_governor_block_has_every_s8_key():
    gov = config.load().governor_settings()
    assert gov.dry_run is True and gov.enabled is False
    assert gov.wind_down_at_percent == {"five_hour": 85, "seven_day": 90}


# ---------------------------------------------------------------- notify settings


def test_notify_settings_default_when_the_toml_has_none():
    n = config.Config().notify_settings()
    assert n.enabled is False and n.dry_run is True
    assert n.needs_you_after_seconds == 60 and n.nudge_minutes == 15 and n.max_nudges == 2
    assert n.present_seconds == 120 and n.quiet_hours == ["23:00", "08:00"]
    assert n.toast_enabled is True and n.ntfy_enabled is False and n.telegram_enabled is False


def test_notify_settings_merges_nested_tables_over_the_defaults():
    cfg = config.Config(notify={
        "enabled": True, "max_nudges": 5,
        "toast": {"enabled": False},
        "ntfy": {"enabled": True, "url": "http://100.1.2.3:2586", "topic": "phone"},
        "telegram": {"enabled": True, "chat_id": "889", "credential_target": "pantheon-telegram"},
    })
    n = cfg.notify_settings()
    assert n.enabled is True and n.max_nudges == 5
    assert n.dry_run is True  # untouched keys keep their default
    assert n.toast_enabled is False
    assert n.ntfy_enabled is True and n.ntfy_url == "http://100.1.2.3:2586" and n.ntfy_topic == "phone"
    assert n.telegram_enabled is True and n.telegram_chat_id == "889"
    assert n.telegram_credential_target == "pantheon-telegram"


def test_real_toml_notify_block_ships_disabled_and_dry():
    n = config.load().notify_settings()
    assert n.enabled is False and n.dry_run is True
    assert n.toast_enabled is True
    assert n.telegram_credential_target == ""  # not yet filled in -- see channels.py docstring


def test_notify_dirs_and_presence_file():
    cfg = config.Config(state_dir="/tmp/pantheon-state")
    assert cfg.notify_dir.name == "notify"
    assert cfg.notify_sent_file.name == "sent.jsonl"
    assert cfg.notify_last_file.name == "last.json"
    assert cfg.presence_file.name == "presence"


# ---------------------------------------------------------------- appearance settings


def test_appearance_settings_default_when_the_toml_has_none():
    app = config.Config().appearance_settings()
    assert app.theme == "pantheon" and app.glyphs == "unicode"
    assert app.clock == "24h"
    assert app.hide == []
    assert app.icons is False


def test_icons_setting_round_trips_through_load(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('[appearance]\nicons = true\n', encoding="utf-8")
    cfg = config.load(p)
    assert cfg.appearance_settings().icons is True


def test_appearance_settings_merges_the_toml_over_the_defaults():
    cfg = config.Config(appearance={"theme": "light"})
    app = cfg.appearance_settings()
    assert app.theme == "light"
    assert app.glyphs == "unicode"       # untouched keys keep their default
    assert app.clock == "24h" and app.hide == []


def test_appearance_block_round_trips_through_load(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text(
        '[appearance]\ntheme = "high-contrast"\nglyphs = "ascii"\n',
        encoding="utf-8",
    )
    cfg = config.load(p)
    app = cfg.appearance_settings()
    assert app.theme == "high-contrast" and app.glyphs == "ascii"


def test_clock_and_hide_settings_round_trip_through_load(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text(
        '[appearance]\nclock = "12h"\nhide = ["today", "trend"]\n',
        encoding="utf-8",
    )
    cfg = config.load(p)
    app = cfg.appearance_settings()
    assert app.clock == "12h"
    assert app.hide == ["today", "trend"]


def test_appearance_from_dict_ignores_unknown_keys():
    # config.py's docstring rule: `Appearance.from_dict` stays tolerant of unknown keys.
    app = config.Appearance.from_dict({"clock": "12h", "made_up": "x"})
    assert app.clock == "12h"


def test_gauge_setting_defaults_to_solid():
    assert config.Appearance().gauge == "solid"
    assert config.Appearance.from_dict({}).gauge == "solid"


def test_gauge_setting_round_trips_through_load(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('[appearance]\ngauge = "blocks"\n', encoding="utf-8")
    cfg = config.load(p)
    assert cfg.appearance_settings().gauge == "blocks"


# ---------------------------------------------------------------- standalone settings


def test_sprints_folder_defaults_to_none_so_it_can_be_derived_from_the_base():
    cfg = config.Config()
    assert cfg.sprints_folder is None
    assert cfg.sprints_dir == Path(cfg.vault) / "Projects" / "Sprints"


def test_standalone_settings_default_when_the_toml_has_none():
    st = config.Config().standalone_settings()
    assert st.folder == config.Standalone().folder


def test_standalone_settings_merges_the_toml_over_the_defaults():
    cfg = config.Config(standalone={"folder": "C:/elsewhere"})
    st = cfg.standalone_settings()
    assert st.folder == "C:/elsewhere"


def test_standalone_block_round_trips_through_load(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text(
        '[standalone]\nfolder = "C:/tasks"\n',
        encoding="utf-8",
    )
    cfg = config.load(p)
    st = cfg.standalone_settings()
    assert st.folder == "C:/tasks"


def test_real_toml_has_a_standalone_block_and_stays_on_obsidian():
    cfg = config.load(config.PROJECT_ROOT / "pantheon.example.toml")
    assert cfg.task_source == "obsidian_base"
    assert cfg.base_file == "Projects/Sprints.base"
    st = cfg.standalone_settings()
    assert st.folder


# ---------------------------------------------------------------- follow-up new_session settings


def test_new_session_settings_default_when_the_toml_has_none():
    ns = config.Config().new_session_settings()
    assert ns.model == "default"
    assert ns.effort == "high"


def test_new_session_settings_merges_the_toml_over_the_defaults():
    cfg = config.Config(new_session={"model": "opus", "effort": "max"})
    ns = cfg.new_session_settings()
    assert ns.model == "opus"
    assert ns.effort == "max"


def test_new_session_block_round_trips_through_load(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('[new_session]\nmodel = "fable"\neffort = "medium"\n', encoding="utf-8")
    cfg = config.load(p)
    ns = cfg.new_session_settings()
    assert ns.model == "fable"
    assert ns.effort == "medium"


def test_new_session_from_dict_ignores_unknown_keys():
    ns = config.NewSession.from_dict({"model": "opus", "bogus": 1})
    assert ns.model == "opus"
    assert ns.effort == "high"


# ---------------------------------------------------------------- keys settings


def test_keys_settings_default_when_the_toml_has_none():
    keys = config.Config().keys_settings()
    assert keys.back_to_deck == "F12"


def test_keys_settings_merges_the_toml_over_the_defaults():
    cfg = config.Config(keys={"back_to_deck": "F9"})
    keys = cfg.keys_settings()
    assert keys.back_to_deck == "F9"


def test_keys_block_round_trips_through_load(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('[keys]\nback_to_deck = "F9"\n', encoding="utf-8")
    cfg = config.load(p)
    assert cfg.keys_settings().back_to_deck == "F9"


def test_real_toml_has_a_keys_block_with_the_f12_default():
    cfg = config.load(config.PROJECT_ROOT / "pantheon.example.toml")
    assert cfg.keys_settings().back_to_deck == "F12"


def test_state_dir_env_override_wins_over_the_file(tmp_path, monkeypatch):
    """The launcher test lane starts a whole throwaway deck; PANTHEON_STATE_DIR keeps its log and
    its events out of the live deck's state folder (tests/test_launcher_tmux.py)."""
    p = tmp_path / "p.toml"
    p.write_text('state_dir = "C:/from-the-file"\n', encoding="utf-8")
    assert config.load(p).state_dir == "C:/from-the-file"
    monkeypatch.setenv("PANTHEON_STATE_DIR", str(tmp_path / "throwaway"))
    cfg = config.load(p)
    assert cfg.state_dir == str(tmp_path / "throwaway")
    assert cfg.log_file == tmp_path / "throwaway" / "pantheon.log"
