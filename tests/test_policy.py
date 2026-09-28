"""The governor's decisions, with no files, no tmux, no clock of
its own -- every case a plain function call with plain data in and plain data out.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pantheon.governor import policy
from pantheon.models import AgentState, AgentStatus

NOW = datetime(2026, 9, 2, 20, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- 1. threshold crossing


def test_threshold_crossed_fires_a_crossing():
    usage = {"five_hour_pct": 86.0, "five_hour_resets_at": "2026-09-03T01:00:00.000Z"}
    got = policy.check_windows(usage, {"five_hour": 85, "seven_day": 90}, {})
    assert len(got) == 1
    assert got[0].window == "five_hour" and got[0].percent == 86.0
    assert got[0].resets_at == "2026-09-03T01:00:00.000Z"


def test_threshold_already_acted_on_for_this_reset_does_not_fire_again():
    usage = {"five_hour_pct": 86.0, "five_hour_resets_at": "2026-09-03T01:00:00.000Z"}
    acted = {"five_hour": "2026-09-03T01:00:00.000Z"}
    assert policy.check_windows(usage, {"five_hour": 85}, acted) == []
    # A NEW reset time (the window turned over) fires again.
    acted_stale = {"five_hour": "2026-09-02T20:00:00.000Z"}
    assert len(policy.check_windows(usage, {"five_hour": 85}, acted_stale)) == 1


def test_percent_unknown_never_crosses():
    assert policy.check_windows({"five_hour_pct": None}, {"five_hour": 85}, {}) == []
    assert policy.check_windows(None, {"five_hour": 85}, {}) == []
    assert policy.check_windows({}, {"five_hour": 85}, {}) == []


def test_below_threshold_does_not_cross():
    assert policy.check_windows({"five_hour_pct": 84.9}, {"five_hour": 85}, {}) == []


def test_both_windows_can_cross_at_once():
    usage = {"five_hour_pct": 91.0, "seven_day_pct": 95.0,
             "five_hour_resets_at": "2026-09-03T01:00:00.000Z", "seven_day_resets_at": "2026-09-07T00:00:00.000Z"}
    got = policy.check_windows(usage, {"five_hour": 85, "seven_day": 90}, {})
    assert {c.window for c in got} == {"five_hour", "seven_day"}


def test_a_provider_usage_object_works_the_same_as_a_dict():
    from pantheon.models import ProviderUsage

    usage = ProviderUsage(provider="claude", five_hour_pct=90.0, five_hour_resets_at="2026-09-03T01:00:00.000Z")
    got = policy.check_windows(usage, {"five_hour": 85}, {})
    assert len(got) == 1 and got[0].percent == 90.0


def test_agents_to_wind_down_skips_codex_and_already_handled_rows():
    rows = [
        AgentState(session_id="a", provider="claude", status=AgentStatus.WORKING),
        AgentState(session_id="b", provider="claude", status=AgentStatus.WAITING_INPUT),
        AgentState(session_id="c", provider="claude", status=AgentStatus.PARKED),
        AgentState(session_id="d", provider="claude", status=AgentStatus.WINDING_DOWN),
        AgentState(session_id="e", provider="claude", status=AgentStatus.GONE),
        AgentState(session_id="f", provider="codex", status=AgentStatus.WORKING),
    ]
    got = {r.session_id for r in policy.agents_to_wind_down(rows)}
    assert got == {"a", "b"}


# ---------------------------------------------------------------- 2. parking


def test_parks_on_session_end_immediately():
    pending = {"s1": {"parked_at_deadline": _iso(NOW + timedelta(minutes=5))}}
    got = policy.parking_decisions(pending, {"s1"}, NOW, set(), max_parked=6)
    assert got == [policy.ParkDecision("s1", True, "checkpointed")]


def test_parks_on_grace_expiry_when_no_session_end_arrived():
    pending = {"s1": {"parked_at_deadline": _iso(NOW - timedelta(seconds=1))}}
    got = policy.parking_decisions(pending, set(), NOW, set(), max_parked=6)
    assert got == [policy.ParkDecision("s1", True, "grace_expired")]


def test_neither_condition_yet_makes_no_decision():
    pending = {"s1": {"parked_at_deadline": _iso(NOW + timedelta(minutes=5))}}
    assert policy.parking_decisions(pending, set(), NOW, set(), max_parked=6) == []


def test_already_parked_is_never_parked_twice():
    pending = {"s1": {"parked_at_deadline": _iso(NOW - timedelta(seconds=1))}}
    assert policy.parking_decisions(pending, {"s1"}, NOW, {"s1"}, max_parked=6) == []


def test_max_parked_reached_refuses_instead_of_parking():
    pending = {
        "s1": {"parked_at_deadline": _iso(NOW - timedelta(seconds=1))},
        "s2": {"parked_at_deadline": _iso(NOW - timedelta(seconds=1))},
    }
    got = policy.parking_decisions(pending, set(), NOW, {"already-1"}, max_parked=1)
    assert got == [policy.ParkDecision("s1", False, "max_parked"), policy.ParkDecision("s2", False, "max_parked")]


# ---------------------------------------------------------------- 3. resume


def test_ready_to_resume_true_only_once_the_time_has_come():
    assert policy.ready_to_resume({"resume_after": _iso(NOW - timedelta(minutes=1))}, NOW) is True
    assert policy.ready_to_resume({"resume_after": _iso(NOW + timedelta(minutes=1))}, NOW) is False
    assert policy.ready_to_resume({}, NOW) is False


def test_already_resumed_by_hand_is_detected_from_a_live_window():
    from pantheon.models import TmuxWindow

    windows = [TmuxWindow(3, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon")]
    got = policy.already_resumed("s1", "C:/Home/x/Documents/Projects/loom-os", windows, [])
    assert got == "the user"


def test_already_resumed_by_the_claude_code_builtin_wait():
    from pantheon.models import Event

    events = [Event(ts="2026-09-02T20:05:00.000Z", event="Notification", session_id="s1",
                    notification_type="quota_auto_resume_fired")]
    assert policy.already_resumed("s1", "C:/nowhere", [], events) == "claude-code-builtin"


def test_not_already_resumed_when_nothing_matches():
    assert policy.already_resumed("s1", "C:/nowhere", [], []) is None


def test_resume_ladder_climbs_one_rung_at_a_time_and_stops_at_four():
    assert policy.next_rung(0) == 1
    assert policy.next_rung(1) == 2
    assert policy.next_rung(2) == 3
    assert policy.next_rung(3) == 4
    assert policy.next_rung(4) == 4  # never retries past the top


def test_resume_order_is_oldest_first():
    parked = [
        {"session_id": "newer", "parked_at": "2026-09-02T20:00:00.000Z"},
        {"session_id": "oldest", "parked_at": "2026-09-02T18:00:00.000Z"},
        {"session_id": "middle", "parked_at": "2026-09-02T19:00:00.000Z"},
    ]
    assert policy.resume_order(parked) == ["oldest", "middle", "newer"]


# ---------------------------------------------------------------- hud staleness


def test_hud_is_stale_past_the_max_age_and_when_unreadable():
    fresh = _iso(NOW - timedelta(seconds=30))
    stale = _iso(NOW - timedelta(seconds=200))
    assert policy.hud_is_stale(fresh, NOW, max_age_seconds=120) is False
    assert policy.hud_is_stale(stale, NOW, max_age_seconds=120) is True
    assert policy.hud_is_stale(None, NOW) is True
    assert policy.hud_is_stale("not a time", NOW) is True
