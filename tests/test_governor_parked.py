"""`state/limits/parked.jsonl` and `pending.json`: the written
record that makes the governor safe across a restart, and that a session is never parked twice.
"""
from __future__ import annotations

from pantheon import config as config_mod
from pantheon.governor import parked as parked_mod


def cfg_for(tmp_path, **kw):
    kw.setdefault("state_dir", str(tmp_path / "state"))
    return config_mod.Config(**kw)


# ---------------------------------------------------------------- pending.json


def test_pending_round_trips_and_survives_a_restart(tmp_path):
    cfg = cfg_for(tmp_path)
    assert parked_mod.read_pending(cfg) == {}
    parked_mod.set_pending(cfg, "s1", {"cwd": "C:/x", "parked_at_deadline": "2026-09-02T20:05:00.000Z"})
    parked_mod.set_pending(cfg, "s2", {"cwd": "C:/y", "parked_at_deadline": "2026-09-02T20:06:00.000Z"})
    reloaded = parked_mod.read_pending(cfg)  # a fresh read, as a restarted governor would do
    assert set(reloaded) == {"s1", "s2"}
    assert reloaded["s1"]["cwd"] == "C:/x"


def test_clear_pending_removes_only_the_named_session(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.set_pending(cfg, "s1", {"cwd": "C:/x"})
    parked_mod.set_pending(cfg, "s2", {"cwd": "C:/y"})
    parked_mod.clear_pending(cfg, "s1")
    assert set(parked_mod.read_pending(cfg)) == {"s2"}
    parked_mod.clear_pending(cfg, "not-there")  # never raises on a session that was not pending


def test_read_pending_survives_a_corrupt_file(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.pending_path(cfg).parent.mkdir(parents=True)
    parked_mod.pending_path(cfg).write_text("not json", encoding="utf-8")
    assert parked_mod.read_pending(cfg) == {}


# ---------------------------------------------------------------- parked.jsonl


def test_a_park_line_carries_all_seven_fields(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.append_park(cfg, parked_mod.Parked(
        session_id="s1", project="loom-os", cwd="C:/x", window_name="loom-os",
        parked_at="2026-09-02T20:10:00.000Z", reason="checkpointed", resume_after="2026-09-03T01:00:00.000Z",
    ))
    lines = parked_mod._read_lines(parked_mod.parked_path(cfg))
    assert len(lines) == 1
    line = lines[0]
    for field in ("session_id", "project", "cwd", "window_name", "parked_at", "reason", "resume_after"):
        assert field in line
    assert line["reason"] == "checkpointed"


def test_a_session_with_no_later_resumed_line_is_still_parked(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.append_park(cfg, parked_mod.Parked(session_id="s1", parked_at="t1", reason="checkpointed"))
    assert "s1" in parked_mod.open_parks(cfg)
    parked_mod.append_resumed(cfg, "s1", by="the user")
    assert "s1" not in parked_mod.open_parks(cfg)


def test_never_park_the_same_session_twice_is_the_callers_job_but_open_parks_reflects_it(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.append_park(cfg, parked_mod.Parked(session_id="s1", parked_at="t1", reason="checkpointed"))
    still_open = parked_mod.open_parks(cfg)
    assert "s1" in still_open  # the runner checks this set before parking again


def test_park_refused_line_does_not_count_as_parked(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.append_park_refused(cfg, "s7", parked_count=6, max_parked=6)
    assert parked_mod.open_parks(cfg) == {}
    lines = parked_mod._read_lines(parked_mod.parked_path(cfg))
    assert lines[0]["kind"] == "park_refused" and lines[0]["max_parked"] == 6


def test_resumed_by_handoff_writes_the_same_resumed_line(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.append_park(cfg, parked_mod.Parked(session_id="s1", parked_at="t1", reason="checkpointed"))
    parked_mod.append_resumed(cfg, "s1", by="handoff")
    lines = parked_mod._read_lines(parked_mod.parked_path(cfg))
    assert lines[-1] == {"kind": "resumed", "session_id": "s1", "resumed_at": lines[-1]["resumed_at"], "by": "handoff"}
    assert "s1" not in parked_mod.open_parks(cfg)


# ---------------------------------------------------------------- dry-run log


def test_dryrun_lines_are_appended_verbatim(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.append_dryrun(cfg, {"event": "would_wind_down", "session_id": "s1"})
    parked_mod.append_dryrun(cfg, {"event": "would_park", "session_id": "s1"})
    lines = parked_mod._read_lines(parked_mod.dryrun_path(cfg))
    assert [l["event"] for l in lines] == ["would_wind_down", "would_park"]


# ---------------------------------------------------------------- HUD summary


def test_summary_counts_winding_down_and_parked(tmp_path):
    cfg = cfg_for(tmp_path, governor={"dry_run": False})
    parked_mod.set_pending(cfg, "s1", {"resume_after": "2026-09-03T02:00:00.000Z"})
    parked_mod.append_park(cfg, parked_mod.Parked(session_id="s2", parked_at="t1", reason="checkpointed",
                                                  resume_after="2026-09-03T01:00:00.000Z"))
    got = parked_mod.summary(cfg)
    assert got == {"winding_down": 1, "parked": 1, "five_hour_resets_at": "2026-09-03T01:00:00.000Z", "dry_run": False}


def test_summary_is_all_zero_and_dry_by_default(tmp_path):
    cfg = cfg_for(tmp_path)
    assert parked_mod.summary(cfg) == {"winding_down": 0, "parked": 0, "five_hour_resets_at": None, "dry_run": True}
