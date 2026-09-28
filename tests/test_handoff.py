"""The trade-off button: digest, briefing, and the dispatch to the other
provider, all against fixtures and a fake provider -- no real pwsh, no real tmux.
"""
from __future__ import annotations

import subprocess

from pantheon import config as config_mod
from pantheon import events as events_mod
from pantheon.governor import handoff
from pantheon.governor import parked as parked_mod
from pantheon.models import AgentState, AgentStatus, LaunchResult, TaskRow, utcnow_iso

NECROMANCY_SAMPLE = """SESSION DIGEST: abc123
file: C:/Home/x/.claude/projects/x/abc123.jsonl (12KB)
span: 2026-09-02T10:00:00Z -> 2026-09-02T10:40:00Z
checkpoint: NEVER written this session

== FIRST ASK ==
fix the Homebase task card overflow on narrow screens

== USER MESSAGES (the decisions; capped 25) ==
- fix the Homebase task card overflow on narrow screens
- also check the mobile layout

== FINAL ASSISTANT STATE (last text blocks) ==
I found the overflow bug in the card renderer and I am about to patch it.

== FILES WRITTEN ==
- src/components/TaskCard.tsx

== FILES READ (capped) ==
- src/components/Board.tsx
- src/styles/card.css

== COMMANDS RUN (capped) ==
- npm test

== GIT / COMMITS ==
- fix(cards): clip overflow on narrow columns

== PRS ==
- pull request #42

== VERSIONS SEEN ==
  v1.2.3

== TEST RESULTS ==
- 12 passed

== ERRORS (first line each, capped) ==
- TypeError: cannot read property 'width' of undefined
"""


def cfg_for(tmp_path, **kw):
    (tmp_path / "projects" / "loom-os").mkdir(parents=True)
    kw.setdefault("state_dir", str(tmp_path / "state"))
    kw.setdefault("projects_root", str(tmp_path / "projects"))
    return config_mod.Config(**kw)


def claude_row(**kw) -> AgentState:
    base = dict(session_id="abc123", provider="claude", status=AgentStatus.GONE,
                project="loom-os", cwd="C:/Home/x/Documents/Projects/loom-os",
                last_action="killed from the deck", last_event_ts=utcnow_iso(), tracker_id="loom-os-fix-cards")
    base.update(kw)
    return AgentState(**base)


def codex_row(**kw) -> AgentState:
    base = dict(session_id="codex-1", provider="codex", status=AgentStatus.FAILED,
                project="loom-os", cwd="C:/Home/x/Documents/Projects/loom-os",
                job_id="codex-20260902T100000Z-loom-os", last_action="codex exec exit code 1",
                last_event_ts=utcnow_iso())
    base.update(kw)
    return AgentState(**base)


# ---------------------------------------------------------------- digest_for: Claude


def test_digest_for_claude_keeps_named_sections_and_drops_the_rest():
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, NECROMANCY_SAMPLE, "")

    digest, source = handoff.digest_for(claude_row(), config_mod.Config(), run_pwsh=fake_run)
    assert "-Project" in calls[0] and "C:/Home/x/Documents/Projects/loom-os" in calls[0]
    assert "-Session" in calls[0] and "abc123" in calls[0]
    assert "FIRST ASK" in digest and "fix the Homebase task card overflow" in digest
    assert "FILES WRITTEN" in digest and "TaskCard.tsx" in digest
    assert "FILES READ" in digest and "Board.tsx" in digest
    assert "GIT / COMMITS" in digest and "clip overflow" in digest
    assert "ERRORS" in digest and "TypeError" in digest
    assert "PRS" not in digest and "pull request #42" not in digest
    assert "VERSIONS SEEN" not in digest and "TEST RESULTS" not in digest
    assert "abc123" in source


def test_digest_for_claude_caps_lines_and_line_length():
    long_line = "x" * 500
    lots = "== FIRST ASK ==\n" + "\n".join(f"item {i} {long_line}" for i in range(100))

    def fake_run(argv):
        return subprocess.CompletedProcess(argv, 0, lots, "")

    digest, _ = handoff.digest_for(claude_row(), config_mod.Config(), run_pwsh=fake_run)
    lines = digest.splitlines()
    assert len(lines) <= handoff.MAX_DIGEST_LINES
    assert all(len(line) <= handoff.MAX_LINE_CHARS for line in lines)


def test_digest_for_claude_nothing_found_when_necromancy_finds_no_session():
    def fake_run(argv):
        return subprocess.CompletedProcess(argv, 1, "NO SESSIONS in C:/x", "")

    digest, source = handoff.digest_for(claude_row(), config_mod.Config(), run_pwsh=fake_run)
    assert digest == "" and source == handoff.NOT_FOUND_SENTENCE


def test_digest_for_claude_nothing_found_when_pwsh_itself_fails():
    def fake_run(argv):
        raise FileNotFoundError("pwsh not found")

    digest, source = handoff.digest_for(claude_row(), config_mod.Config(), run_pwsh=fake_run)
    assert digest == "" and source == handoff.NOT_FOUND_SENTENCE


def test_digest_for_claude_nothing_found_without_a_cwd_or_session_id():
    row = claude_row(cwd=None)
    digest, source = handoff.digest_for(row, config_mod.Config())
    assert digest == "" and source == handoff.NOT_FOUND_SENTENCE


# ---------------------------------------------------------------- digest_for: Codex


def test_digest_for_codex_reads_the_last_lines_of_its_own_log(tmp_path):
    cfg = cfg_for(tmp_path)
    log_lines = [f"line {i}" for i in range(200)]
    (cfg.dispatch_dir).mkdir(parents=True, exist_ok=True)
    (cfg.dispatch_dir / "codex-20260902T100000Z-loom-os.log").write_text("\n".join(log_lines), encoding="utf-8")
    digest, source = handoff.digest_for(codex_row(), cfg)
    assert "line 199" in digest and "line 0" not in digest  # tail, not head
    assert "codex-20260902T100000Z-loom-os.log" in source


def test_digest_for_codex_nothing_found_without_a_log_file(tmp_path):
    cfg = cfg_for(tmp_path)
    digest, source = handoff.digest_for(codex_row(), cfg)
    assert digest == "" and source == handoff.NOT_FOUND_SENTENCE


# ---------------------------------------------------------------- handoff_briefing


def test_handoff_briefing_uses_the_row_template_when_dispatched_from_a_queue_row(tmp_path):
    cfg = cfg_for(tmp_path)
    origin = TaskRow(id="Fix the cards", summary="Fix the cards", project="loom-os", status="open",
                     path="C:/vault/Fix the cards.md")
    text = handoff.handoff_briefing(origin, claude_row(), "the digest text", "claude", "codex",
                                    "a usage limit", cfg)
    assert 'working the Sprint "Fix the cards"' in text          # the template came first
    assert "Previous agent: claude" in text and "a usage limit" in text
    assert "the digest text" in text
    assert "Continue from where the previous agent stopped" in text
    assert "say DONE" in text


def test_handoff_briefing_stands_in_a_header_without_an_origin_row(tmp_path):
    cfg = cfg_for(tmp_path)
    text = handoff.handoff_briefing(None, claude_row(), "no transcript was found", "claude", "codex",
                                    "it was killed or its window closed", cfg)
    assert "You are continuing work in project loom-os" in text
    assert "loom-os-fix-cards" in text  # the tracker id names the previous session
    assert "no transcript was found" in text


# ---------------------------------------------------------------- handoff()


class FakeProvider:
    def __init__(self, name: str, ok: bool = True):
        self.name = name
        self.ok = ok
        self.calls: list[tuple] = []

    def capabilities(self):
        return {"interactive_tmux"} if self.name == "claude" else {"headless"}

    def launch(self, project_dir, briefing_path, interactive):
        self.calls.append((project_dir, briefing_path, interactive))
        if not self.ok:
            return LaunchResult(False, message="the fake provider refused")
        if self.name == "codex":
            return LaunchResult(True, "headless", 6, "codex:loom-os", job_id="codex-2")
        return LaunchResult(True, "tmux", 6, "loom-os", tmux_pane="%11", message="claude is working in window 6")


def _no_digest(row, cfg):
    return "", "no transcript was found for the previous agent; start from the row and CHECKPOINT.md"


def test_handoff_dispatches_with_the_previous_agent_paragraph_and_logs_one_line(tmp_path):
    cfg = cfg_for(tmp_path)
    codex = FakeProvider("codex")
    result = handoff.handoff(claude_row(), "codex", cfg, {"codex": codex}, [], digest_fn=_no_digest)
    assert result.ok and result.job_id == "codex-2"
    project_dir, briefing_path, interactive = codex.calls[0]
    text = open(briefing_path, encoding="utf-8").read()
    assert "Previous agent: claude" in text
    assert "no transcript was found" in text
    events, errors = events_mod.read_events(cfg.events_file)
    assert errors == 0
    handoff_events = [e for e in events if e.event == "handoff"]
    assert len(handoff_events) == 1
    e = handoff_events[0]
    assert e.session_id == "abc123" and e.extra["to_provider"] == "codex" and e.extra["from_provider"] == "claude"
    assert e.extra["tracker_id"] == "loom-os-fix-cards"


def test_handoff_from_codex_to_claude_carries_the_log_tail(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.dispatch_dir.mkdir(parents=True, exist_ok=True)
    (cfg.dispatch_dir / "codex-20260902T100000Z-loom-os.log").write_text("...\nlast line of the log", encoding="utf-8")
    claude = FakeProvider("claude")
    result = handoff.handoff(codex_row(), "claude", cfg, {"claude": claude}, [])
    assert result.ok
    briefing_path = claude.calls[0][1]
    text = open(briefing_path, encoding="utf-8").read()
    assert "last line of the log" in text
    assert "Previous agent: codex" in text


def test_handoff_marks_a_parked_row_resumed_so_the_governor_never_also_resumes_it(tmp_path):
    cfg = cfg_for(tmp_path)
    parked_mod.append_park(cfg, parked_mod.Parked(session_id="abc123", parked_at="t1", reason="checkpointed"))
    codex = FakeProvider("codex")
    result = handoff.handoff(claude_row(status=AgentStatus.PARKED), "codex", cfg, {"codex": codex}, [],
                             digest_fn=_no_digest)
    assert result.ok
    assert "abc123" not in parked_mod.open_parks(cfg)
    lines = parked_mod._read_lines(parked_mod.parked_path(cfg))
    assert lines[-1]["by"] == "handoff"


def test_handoff_refused_when_the_provider_is_not_switched_on(tmp_path):
    cfg = cfg_for(tmp_path)
    result = handoff.handoff(claude_row(), "codex", cfg, {"claude": FakeProvider("claude")}, [], digest_fn=_no_digest)
    assert result.ok is False and "codex" in result.message


def test_handoff_refused_when_max_agents_reached(tmp_path):
    cfg = cfg_for(tmp_path)
    busy = [AgentState(session_id=f"s{i}", provider="claude", status=AgentStatus.WORKING) for i in range(4)]
    codex = FakeProvider("codex")
    result = handoff.handoff(claude_row(), "codex", cfg, {"codex": codex}, busy, digest_fn=_no_digest)
    assert result.ok is False and "already working" in result.message
    assert codex.calls == []  # nothing was written


def test_handoff_never_touches_the_old_row(tmp_path):
    """The old session id gets a `handoff` event, not a `dispatch` line pretending to be it --
    `supervisor.state._fold_pantheon` is what actually leaves the row alone; this just proves
    the event this module writes carries the OLD session id, per section 11."""
    cfg = cfg_for(tmp_path)
    codex = FakeProvider("codex")
    handoff.handoff(claude_row(), "codex", cfg, {"codex": codex}, [], digest_fn=_no_digest)
    events, _ = events_mod.read_events(cfg.events_file)
    handoff_event = next(e for e in events if e.event == "handoff")
    assert handoff_event.session_id == "abc123"
