"""A grouped tmux client session shares every window with the base session, so `list-windows -a`
reports each pane twice; the deck must show one row per pane."""
from __future__ import annotations

from datetime import datetime, timezone

from pantheon import tmuxctl
from pantheon.models import TmuxWindow
from pantheon.supervisor import state


def _w(session, pane="%7", index=4, name="Claude", cmd="claude"):
    return TmuxWindow(index, name, cmd, "/c/Home/x/Documents/Projects", pane, session)


def test_dedupe_keeps_the_base_session_copy_whatever_the_order():
    grouped_first = [_w("pantheon-4756"), _w("pantheon")]
    kept = tmuxctl.dedupe_grouped(grouped_first)
    assert [w.session for w in kept] == ["pantheon"]
    base_first = [_w("pantheon"), _w("pantheon-4756")]
    assert [w.session for w in tmuxctl.dedupe_grouped(base_first)] == ["pantheon"]


def test_dedupe_leaves_distinct_panes_and_idless_windows_alone():
    windows = [_w("pantheon", pane="%7"), _w("pantheon", pane="%9", index=5), _w("other", pane="")]
    assert tmuxctl.dedupe_grouped(windows) == windows


def test_fold_never_emits_two_unknown_rows_for_one_pane():
    now = datetime(2026, 9, 5, 1, 26, tzinfo=timezone.utc)
    rows = state.fold([], [_w("pantheon"), _w("pantheon-4756")], now, pantheon_session="pantheon")
    keys = [r.session_id for r in rows]
    assert keys.count("pane:%7") == 1 and len(keys) == len(set(keys))
