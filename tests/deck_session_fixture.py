"""The three sessions the deck's committed snapshot and `docs/renders/` are drawn from.

Not a test -- a fixture module, imported by `tests/test_snapshots.py` and by
`docs/renders/render_desk.py` so both draw the SAME wall of conversations.

It exists because the sidebar's real source (`session_view/recent.py`) scans the user's own
`~/.claude/projects` and `~/.codex/sessions` and stamps each row with a file's mtime. A snapshot
built on that would differ every run and every machine, and a committed render would quietly
publish whatever he happened to be working on. Handing the sidebar a fixed list instead makes
both deterministic and keeps his real transcripts out of the repo entirely.

The transcripts these rows point at are package A's synthetic fixtures
(`tests/fixtures/session_view/*.jsonl`) -- built from the record shapes the parsers read, never
copies of a real session.
"""
from __future__ import annotations

from pathlib import Path

from pantheon.session_view import models as m

FIXTURES = Path(__file__).parent / "fixtures"
# Package A's parser fixtures: every record shape a Claude transcript and a Codex rollout can
# carry, so the first card exercises the whole chip vocabulary.
CLAUDE_TRANSCRIPT = str(FIXTURES / "session_view" / "claude_session.jsonl")
CODEX_ROLLOUT = str(FIXTURES / "session_view" / "codex_rollout.jsonl")
# A second, ordinary Claude session -- a real-shaped debugging turn -- so the wall does not draw
# the same conversation twice and read like a rendering bug.
CLAUDE_SECOND = str(FIXTURES / "deck_wall" / "hikinglog_session.jsonl")

PROJECTS_ROOT = "C:/Home/x/Documents/Projects"

# One frozen instant for the RECENT row's "last touched", so nothing here reads a clock.
FROZEN_ISO = "2026-09-01T23:40:00Z"


def _cwd(project: str) -> str:
    return f"{PROJECTS_ROOT}/{project}"


def live_entries() -> list:
    """Three running sessions, in the order THE PIT sorts them: whoever needs the user first.

    One waiting on a permission prompt, one working, one headless Codex job -- the three shapes
    a tile has to draw (a caution band, a plain chat, and a read-only Codex rollout)."""
    return [
        m.SessionEntry(
            session_id="life0sess1234567", group=m.LIVE, project="loom-os", cwd=_cwd("loom-os"),
            title="Launcher check session", status_text="needs you", needs_human=True,
            window_index=3, tmux_session="pantheon", transcript_path=CLAUDE_TRANSCRIPT,
        ),
        m.SessionEntry(
            session_id="hikinglogsess001", group=m.LIVE, project="hiking_log_v2",
            cwd=_cwd("hiking_log_v2"), title="Spoiler dial leak", status_text="working",
            window_index=4, tmux_session="pantheon", transcript_path=CLAUDE_SECOND,
        ),
        m.SessionEntry(
            session_id="codexjobrunning1", group=m.LIVE, project="pottery_studios",
            cwd=_cwd("pottery_studios"), title="repo sweep", status_text="working",
            provider="codex", transcript_path=CODEX_ROLLOUT,
        ),
    ]


def recent_entries() -> list:
    """One finished transcript, so the sidebar's RECENT section is not an empty heading."""
    return [
        m.SessionEntry(
            session_id="guides0sess98765", group=m.RECENT, project="Gazebo", cwd=_cwd("Gazebo"),
            title="Spoiler dial leak", transcript_path=CLAUDE_SECOND,
            modified_ts=FROZEN_ISO,
        ),
    ]


def entries(_rows=None) -> list:
    """The shape `SessionSidebar(entries_source=...)` expects: live rows, then recent ones."""
    return live_entries() + recent_entries()


def projects() -> list:
    """the sidebar's MORE PROJECTS section -- two fixed workspace projects with no recent
    conversation, instead of a scan of the user's real workspace folder."""
    from pantheon.dispatch.projects import Project

    return [Project("Canvas", _cwd("Canvas"), None), Project("garden_plans", _cwd("garden_plans"), None)]
