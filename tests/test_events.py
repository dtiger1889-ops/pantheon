import json

from pantheon.events import append_event, by_session, derive_project, norm_path, read_events, rotate_if_large
from pantheon.models import AgentStatus, Event, format_age

FIXTURE = [
    {"ts": "2026-09-01T20:00:00.000Z", "source": "claude", "event": "SessionStart", "session_id": "s1",
     "cwd": r"C:\Home\x\Documents\Projects\hiking_log_v2", "detail": "startup", "tmux_pane": "%3"},
    {"ts": "2026-09-01T20:00:05.000Z", "source": "claude", "event": "PostToolUse", "session_id": "s1",
     "cwd": r"C:\Home\x\Documents\Projects\hiking_log_v2", "tool_name": "Read"},
    {"ts": "2026-09-01T20:01:00.000Z", "source": "claude", "event": "Notification", "session_id": "s1",
     "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash"},
    {"ts": "2026-09-01T20:02:00.000Z", "source": "claude", "event": "Stop", "session_id": "s1"},
    {"ts": "2026-09-01T20:03:00.000Z", "source": "codex", "event": "queued", "job_id": "j1", "project": "Plumb"},
    {"ts": "2026-09-01T20:03:01.000Z", "source": "codex", "event": "running", "job_id": "j1"},
    {"ts": "2026-09-01T20:09:00.000Z", "source": "codex", "event": "done", "job_id": "j1", "detail": "exit 0",
     "unknown_future_field": 42},
    {"ts": "2026-09-01T20:10:00.000Z", "source": "pantheon", "event": "kill", "session_id": "s1"},
    {"ts": "2026-09-01T20:11:00.000Z", "source": "claude", "event": "SessionEnd", "session_id": "s1", "detail": "other"},
]


def write_fixture(path, corrupt=True):
    with open(path, "w", encoding="utf-8") as fh:
        for d in FIXTURE:
            fh.write(json.dumps(d) + "\n")
        if corrupt:
            fh.write("{this is not json\n")
            fh.write("[1,2,3]\n")
            fh.write("\n")


def test_read_events_tolerates_corrupt_lines(tmp_path):
    p = tmp_path / "events.jsonl"
    write_fixture(p)
    events, errors = read_events(p)
    assert len(events) == len(FIXTURE)
    assert errors == 2
    assert events[6].extra == {"unknown_future_field": 42}
    assert events[0].when is not None and events[0].when.year == 2026


def test_missing_file_is_empty(tmp_path):
    assert read_events(tmp_path / "nope.jsonl") == ([], 0)


def test_append_roundtrip(tmp_path):
    p = tmp_path / "e.jsonl"
    append_event(p, Event(ts="2026-09-01T00:00:00.000Z", event="dispatch", source="pantheon", session_id="x",
                          extra={"row_file": "a.md"}))
    append_event(p, {"event": "wind_down", "session_id": "x"})
    events, errors = read_events(p)
    assert errors == 0
    assert events[0].extra["row_file"] == "a.md"
    assert events[1].source == "pantheon" and events[1].ts


def test_by_session_groups_jobs_and_sessions(tmp_path):
    p = tmp_path / "e.jsonl"
    write_fixture(p, corrupt=False)
    groups = by_session(read_events(p)[0])
    assert set(groups) == {"s1", "j1"}
    assert [e.event for e in groups["j1"]] == ["queued", "running", "done"]


def test_rotate(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("x" * 100, encoding="utf-8")
    assert rotate_if_large(p, max_bytes=1000) is None
    archived = rotate_if_large(p, max_bytes=10)
    assert archived is not None and archived.exists() and not p.exists()
    assert archived.name.startswith("events-") and archived.suffix == ".jsonl"


def test_norm_path_and_derive_project():
    root = "C:/Home/x/Documents/Projects"
    assert norm_path("c:\\Home\\x\\Documents\\Projects\\") == norm_path("/c/Home/x/Documents/Projects")
    assert derive_project(r"C:\Home\x\Documents\Projects\hiking_log_v2\src", root) == "hiking_log_v2"
    assert derive_project("C:/Home/x/Documents/Projects/Plumb", root) == "Plumb"
    assert derive_project("C:/Home/x/elsewhere", root) is None
    assert derive_project(None, root) is None
    assert derive_project(root, root) is None


def test_status_labels_and_age():
    assert AgentStatus.WAITING_INPUT.needs_human and AgentStatus.BLOCKED_PERMISSION.needs_human
    assert not AgentStatus.WORKING.needs_human
    assert AgentStatus.WAITING_INPUT.label == "waiting - needs input"
    assert format_age(None) == "?" and format_age(30) == "<1m" and format_age(240) == "4m"
    assert format_age(72 * 60) == "1h12m"
