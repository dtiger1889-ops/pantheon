"""Dispatch: the briefing, the gate, the launch record, and the `dispatched` marks."""
from __future__ import annotations

import json

from pantheon import config as config_mod
from pantheon import events as events_mod
from pantheon.dispatch import briefing, launch, marks
from pantheon.models import AgentState, AgentStatus, Event, LaunchResult, TaskRow, TmuxWindow, utcnow_iso


def cfg_for(tmp_path):
    (tmp_path / "projects" / "hiking_log_v2").mkdir(parents=True)
    return config_mod.Config(projects_root=str(tmp_path / "projects"), state_dir=str(tmp_path / "state"),
                             vault=str(tmp_path / "vault"))


def row_for(**kw) -> TaskRow:
    base = dict(id="Rebuild the photo archive index", summary="Rebuild the photo archive index",
                project="hiking_log_v2", tier="soon", complexity="moderate", est_context="small",
                status="open", agent=True, reply="try the old script first", note=None,
                source="[[Workshop notes index]]", path="C:/vault/Projects/Sprints/Rebuild the photo archive index.md")
    base.update(kw)
    return TaskRow(**base)


# ---------------------------------------------------------------- briefing


def test_briefing_fills_every_placeholder_from_the_row(tmp_path):
    cfg = cfg_for(tmp_path)
    text = briefing.build(row_for(), cfg)
    assert "Tracker id: hiking_log_v2-rebuild-the-photo-archive-index" in text
    assert 'working the Sprint "Rebuild the photo archive index" for project hiking_log_v2' in text
    assert "tier soon, effort moderate, est_context small, status open" in text
    assert "try the old script first" in text
    assert "Note on the row: none." in text
    assert "[[Workshop notes index]]" in text and cfg.vault in text
    assert "Do NOT edit the Sprint row file" in text
    assert "{" not in text and "}" not in text          # nothing left unfilled


def test_briefing_file_lands_in_state_dispatch_and_types_as_one_line(tmp_path):
    cfg = cfg_for(tmp_path)
    path = briefing.write(row_for(), cfg)
    assert path.parent == cfg.dispatch_dir and path.suffix == ".md"
    assert path.name.endswith("-rebuild-the-photo-archive-index.md")
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") >= 7
    assert "\n" not in briefing.one_line(text)


def test_gate_refuses_with_a_sentence_that_says_what_to_do(tmp_path):
    cfg = cfg_for(tmp_path)
    assert briefing.is_ready(row_for(), cfg) == (True, "")
    ok, why = briefing.is_ready(row_for(summary=""), cfg)
    assert not ok and "no summary" in why
    ok, why = briefing.is_ready(row_for(project=None), cfg)
    assert not ok and "no project" in why
    ok, why = briefing.is_ready(row_for(project="personal"), cfg)
    assert not ok and why == "no project folder for 'personal'; open it by hand"
    ok, why = briefing.is_ready(None, cfg)
    assert not ok and why == "no task selected"


# ---------------------------------------------------------------- guardrails


def _busy(n: int) -> list[AgentState]:
    return [AgentState(session_id=f"s{i}", provider="claude", status=AgentStatus.WORKING) for i in range(n)]


def test_guard_counts_only_busy_agents_against_the_limit(tmp_path):
    cfg = cfg_for(tmp_path)
    rows = _busy(3) + [AgentState(session_id="w", provider="claude", status=AgentStatus.WAITING_INPUT)]
    assert launch.guard(row_for(), cfg, rows, "claude", ["claude", "codex"]) is None
    refusal = launch.guard(row_for(), cfg, _busy(4), "claude", ["claude"])
    assert refusal is not None and "4 agents are already working (limit 4" in refusal.reason
    assert refusal.needs_confirm is False


def test_guard_asks_before_working_a_row_that_is_not_on_the_agents_plate(tmp_path):
    cfg = cfg_for(tmp_path)
    refusal = launch.guard(row_for(agent=False), cfg, [], "claude", ["claude"])
    assert refusal is not None and refusal.needs_confirm and "not the Agent's plate" in refusal.reason
    assert launch.guard(row_for(agent=False), cfg, [], "claude", ["claude"], confirmed=True) is None


def test_guard_names_the_switched_off_provider(tmp_path):
    cfg = cfg_for(tmp_path)
    refusal = launch.guard(row_for(), cfg, [], "ollama", ["claude", "codex"])
    assert refusal is not None and "ollama" in refusal.reason and "available: claude, codex" in refusal.reason


# ---------------------------------------------------------------- dispatch records


class FakeProvider:
    name = "claude"

    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {"interactive_tmux"}

    def launch(self, project_dir, briefing_path, interactive):
        self.calls.append((project_dir, briefing_path, interactive))
        return LaunchResult(True, "tmux", 5, "hiking_log_v2", tmux_pane="%9", message="claude is working in window 5")


def test_dispatch_writes_the_briefing_launches_and_logs_one_line(tmp_path):
    cfg = cfg_for(tmp_path)
    provider = FakeProvider()
    result = launch.dispatch(row_for(), cfg, provider)
    assert result.ok and result.window_index == 5
    project_dir, briefing_path, interactive = provider.calls[0]
    assert project_dir.endswith("hiking_log_v2") and interactive is True
    assert briefing_path.endswith(".md") and "Tracker id:" in open(briefing_path, encoding="utf-8").read()
    events, errors = events_mod.read_events(cfg.events_file)
    assert errors == 0 and len(events) == 1
    e = events[0]
    assert e.source == "pantheon" and e.event == "dispatch" and e.tmux_pane == "%9"
    assert e.extra["tracker_id"] == "hiking_log_v2-rebuild-the-photo-archive-index"
    assert e.extra["row_id"] == "Rebuild the photo archive index" and e.extra["provider"] == "claude"
    assert e.extra["window_index"] == 5


def test_the_vault_is_never_touched(tmp_path):
    cfg = cfg_for(tmp_path)
    vault = tmp_path / "vault" / "Projects" / "Sprints"
    vault.mkdir(parents=True)
    before = sorted(p.name for p in vault.iterdir())
    launch.dispatch(row_for(), cfg, FakeProvider())
    assert sorted(p.name for p in vault.iterdir()) == before


# ---------------------------------------------------------------- marks


def _dispatch_event(ts: str, row_id="Rebuild the photo archive index", pane="%9", job_id=None, where="tmux"):
    return Event(ts=ts, event="dispatch", source="pantheon", tmux_pane=pane, job_id=job_id,
                 extra={"row_id": row_id, "tracker_id": "t", "provider": "codex" if job_id else "claude",
                        "where": where, "window_index": 5})


def test_a_dispatched_row_is_marked_until_its_session_ends():
    windows = [TmuxWindow(5, "hiking_log_v2", "node", "C:/x", "%9", "pantheon")]
    evs = [_dispatch_event("2026-09-01T10:00:00.000Z")]
    got = marks.dispatch_marks(evs, windows)
    assert "Rebuild the photo archive index" in got
    assert got["Rebuild the photo archive index"].label().startswith("dispatched to claude, window 5,")
    evs.append(Event(ts="2026-09-01T10:30:00.000Z", event="SessionEnd", source="claude", session_id="abc", tmux_pane="%9"))
    assert marks.dispatch_marks(evs, windows) == {}


def test_a_mark_clears_when_the_window_is_gone_or_the_codex_job_finishes():
    evs = [_dispatch_event("2026-09-01T10:00:00.000Z")]
    other_window = [TmuxWindow(0, "deck", "python", "C:/x", "%1", "pantheon")]
    assert marks.dispatch_marks(evs, other_window) == {}          # pane %9 no longer exists
    job = [_dispatch_event("2026-09-01T10:00:00.000Z", pane="%3", job_id="codex-1", where="headless")]
    assert "Rebuild the photo archive index" in marks.dispatch_marks(job, [])
    job.append(Event(ts="2026-09-01T10:05:00.000Z", event="done", source="codex", job_id="codex-1", detail="exit code 0"))
    assert marks.dispatch_marks(job, []) == {}


def test_a_native_pc_window_is_never_claimed_as_live():
    evs = [_dispatch_event("2026-09-01T10:00:00.000Z", pane=None, where="pc-window")]
    assert marks.dispatch_marks(evs, []) == {}
