"""`sent_line` -- the status-line formatter `deck/app.py` will call once wired. Pure formatting only.
"""
from __future__ import annotations

from pantheon.notify import line


def test_sent_line_shape():
    assert line.sent_line("Claude needs you", "permission in loom-os (window 3), 1m", "toast") == (
        "sent: Claude needs you - permission in loom-os (window 3), 1m -> toast"
    )


def test_sent_line_clips_to_max_chars():
    long_body = "x" * 200
    out = line.sent_line("Title", long_body, "ntfy", max_chars=40)
    assert len(out) == 40


def test_sent_line_appends_deck_only_detail_in_parens():
    out = line.sent_line("Codex job failed", "loom-os, exit code 1", "toast",
                         detail="job-7.log", max_chars=200)
    assert out == "sent: Codex job failed - loom-os, exit code 1 (job-7.log) -> toast"
