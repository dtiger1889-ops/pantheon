"""The rules table, one test per row (positive + the present-suppressed negative where the
table has one) plus dedupe, the nudge ladder, and quiet hours. Every test is pure -- no files, no tmux, no clock but a
fixed `NOW` passed in explicitly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pantheon.config import Notify
from pantheon.models import AgentState, AgentStatus
from pantheon.notify import presence as presence_mod
from pantheon.notify import rules

NOW = datetime(2026, 9, 2, 20, 0, 0, tzinfo=timezone.utc)
SETTINGS = Notify(needs_you_after_seconds=60, nudge_minutes=15, max_nudges=2, present_seconds=120,
                  quiet_hours=["23:00", "08:00"])

PRESENT = presence_mod.Presence(client_attached=True, deck_touched_at=NOW.isoformat().replace("+00:00", "Z"))
AWAY = presence_mod.Presence(client_attached=False)


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def t(to_status, age_seconds=0.0, from_status=None, project="loom-os", window="3", **extra_kw):
    tracker_id = extra_kw.pop("tracker_id", None)
    job_id = extra_kw.pop("job_id", None)
    return rules.Transition(
        session_id="s1", to_status=to_status, project=project, session_name="loom-os",
        window=window, from_status=from_status, age_seconds=age_seconds,
        tracker_id=tracker_id, job_id=job_id, extra=extra_kw,
    )


# ---------------------------------------------------------------- rows 1-2: needs-you + nudge


def test_needs_you_fires_after_threshold_when_not_present():
    n = rules.decide(t("blocked-permission", age_seconds=61), AWAY, SETTINGS, {}, now=NOW)
    assert n is not None
    assert n.title == "Claude needs you"
    assert "permission" in n.body and "loom-os" in n.body and "window 3" in n.body
    assert n.tier == rules.TIER_CAUTION
    assert n.channels == ("toast", "ntfy")


def test_needs_you_suppressed_when_present():
    n = rules.decide(t("blocked-permission", age_seconds=120), PRESENT, SETTINGS, {}, now=NOW)
    assert n is None


def test_needs_you_not_yet_old_enough_returns_none():
    n = rules.decide(t("blocked-permission", age_seconds=5), AWAY, SETTINGS, {}, now=NOW)
    assert n is None


def test_needs_you_waiting_input_says_waiting_not_permission():
    n = rules.decide(t("waiting-input", age_seconds=90), AWAY, SETTINGS, {}, now=NOW)
    assert n is not None and "waiting" in n.body and "permission" not in n.body


def test_same_transition_twice_before_nudge_window_is_none():
    key = "s1:blocked-permission"
    history = {key: {"last_notified_at": _iso(NOW - timedelta(minutes=1)), "nudges_sent": 0}}
    n = rules.decide(t("blocked-permission", age_seconds=120), AWAY, SETTINGS, history, now=NOW)
    assert n is None  # already notified 1 minute ago; nudge window is 15 minutes


def test_nudge_fires_after_nudge_minutes_and_wording_names_the_age():
    key = "s1:waiting-input"
    history = {key: {"last_notified_at": _iso(NOW - timedelta(minutes=16)), "nudges_sent": 0}}
    n = rules.decide(t("waiting-input", age_seconds=16 * 60), AWAY, SETTINGS, history, now=NOW)
    assert n is not None and n.title == "Still waiting" and "16m" in n.body
    assert n.channels == ("ntfy",)


def test_nudge_stops_at_max_nudges():
    key = "s1:waiting-input"
    history = {key: {"last_notified_at": _iso(NOW - timedelta(minutes=16)), "nudges_sent": 2}}
    n = rules.decide(t("waiting-input", age_seconds=32 * 60), AWAY, SETTINGS, history, now=NOW)
    assert n is None  # max_nudges is 2; a third would over-notify


def test_nudge_suppressed_when_present():
    key = "s1:waiting-input"
    history = {key: {"last_notified_at": _iso(NOW - timedelta(minutes=16)), "nudges_sent": 0}}
    n = rules.decide(t("waiting-input", age_seconds=16 * 60), PRESENT, SETTINGS, history, now=NOW)
    assert n is None


# ---------------------------------------------------------------- row 3: Codex job failed


def test_failed_fires_immediately_even_when_present():
    n = rules.decide(t("failed", exit_code=1, job_id="job-7"), PRESENT, SETTINGS, {}, now=NOW)
    assert n is not None
    assert n.title == "Codex job failed"
    assert "exit code 1" in n.body
    assert "job-7" in n.detail and "job-7" not in n.body  # the log path is deck-only, not pushed
    assert n.tier == rules.TIER_WARNING
    assert n.channels == ("toast", "ntfy", "telegram")


# ---------------------------------------------------------------- row 4: done / gone with a tracker


def test_done_fires_when_not_present():
    n = rules.decide(t("done", tracker_id="row-1", summary="Fix Homebase task card overflow"),
                     AWAY, SETTINGS, {}, now=NOW)
    assert n is not None and n.title == "Done"
    assert "Fix Homebase task card overflow" in n.body and "loom-os" in n.body
    assert n.tier == rules.TIER_SILENT and n.channels == ("ntfy",)


def test_done_suppressed_when_present():
    n = rules.decide(t("done", tracker_id="row-1"), PRESENT, SETTINGS, {}, now=NOW)
    assert n is None


def test_gone_without_tracker_id_never_fires_as_done():
    """A row going `gone` with no `tracker_id` was never dispatched from the queue -- only
    notifies "done" for a tracked task finishing, never for every closed window."""
    n = rules.decide(t("gone"), AWAY, SETTINGS, {}, now=NOW)
    assert n is None


# ---------------------------------------------------------------- row 6: governor park / resume / resume-failed


def test_parked_notification_names_the_resume_clock():
    resume_at = _iso(NOW + timedelta(hours=5))
    n = rules.decide(t("parked", resume_after=resume_at), AWAY, SETTINGS, {}, now=NOW)
    assert n is not None and n.title == "Parked" and "resumes ~" in n.body
    assert n.tier == rules.TIER_SILENT


def test_parked_notification_honours_the_twelve_hour_clock_setting():
    resume_at = _iso(NOW + timedelta(hours=5))
    n24 = rules.decide(t("parked", resume_after=resume_at), AWAY, SETTINGS, {}, now=NOW)
    n12 = rules.decide(t("parked", resume_after=resume_at), AWAY, SETTINGS, {}, now=NOW, clock="12h")
    assert n24.body != n12.body
    assert ("am" in n12.body or "pm" in n12.body)


def test_resumed_from_parked_is_silent():
    n = rules.decide(t("working", from_status="parked"), AWAY, SETTINGS, {}, now=NOW)
    assert n is not None and n.title == "Resumed" and n.tier == rules.TIER_SILENT


def test_working_from_something_else_is_not_a_resume():
    n = rules.decide(t("working", from_status="winding-down"), AWAY, SETTINGS, {}, now=NOW)
    assert n is None


def test_resume_failed_is_caution_and_names_needs_you():
    n = rules.decide(t("resume-failed"), AWAY, SETTINGS, {}, now=NOW)
    assert n is not None and n.tier == rules.TIER_CAUTION and "needs you" in n.title


# ---------------------------------------------------------------- restart safety: one-shot dedupe
#


def test_failed_does_not_refire_once_already_in_history():
    key = "s1:failed"
    n = rules.decide(t("failed"), AWAY, SETTINGS, {key: {"last_notified_at": _iso(NOW)}}, now=NOW)
    assert n is None


def test_parked_does_not_refire_once_already_in_history():
    key = "s1:parked"
    n = rules.decide(t("parked"), AWAY, SETTINGS, {key: {"last_notified_at": _iso(NOW)}}, now=NOW)
    assert n is None


def test_resume_failed_does_not_refire_once_already_in_history():
    key = "s1:resume-failed"
    n = rules.decide(t("resume-failed"), AWAY, SETTINGS, {key: {"last_notified_at": _iso(NOW)}}, now=NOW)
    assert n is None


# ---------------------------------------------------------------- row 5: budget band crossings


def test_budget_crossing_70_is_caution():
    hud = {"claude": {"five_hour_pct": 72.0, "five_hour_resets_at": _iso(NOW + timedelta(hours=2))}}
    out = rules.budget_events({"claude": {"five_hour_pct": 60.0}}, hud, {})
    assert len(out) == 1 and out[0].tier == rules.TIER_CAUTION and "90%" not in out[0].title


def test_budget_crossing_90_to_100_is_warning():
    hud = {"claude": {"seven_day_pct": 100.0, "seven_day_resets_at": _iso(NOW + timedelta(days=1))}}
    out = rules.budget_events({"claude": {"seven_day_pct": 95.0}}, hud, {})
    assert len(out) == 1 and out[0].tier == rules.TIER_WARNING


def test_budget_no_crossing_when_band_unchanged():
    hud = {"claude": {"five_hour_pct": 75.0}}
    out = rules.budget_events({"claude": {"five_hour_pct": 71.0}}, hud, {})
    assert out == []  # both readings are in the 70 band; nothing new to say


def test_budget_crossing_honours_the_twelve_hour_clock_setting():
    resets = _iso(NOW + timedelta(hours=2))
    hud = {"claude": {"five_hour_pct": 72.0, "five_hour_resets_at": resets}}
    out24 = rules.budget_events({"claude": {"five_hour_pct": 60.0}}, hud, {})
    out12 = rules.budget_events({"claude": {"five_hour_pct": 60.0}}, hud, {}, clock="12h")
    assert out24[0].body != out12[0].body
    assert ("am" in out12[0].body or "pm" in out12[0].body)


def test_budget_dedupe_same_band_same_reset_does_not_refire():
    resets = _iso(NOW + timedelta(hours=2))
    hud = {"claude": {"five_hour_pct": 92.0, "five_hour_resets_at": resets}}
    key = f"budget:claude:five_hour:90:{resets}"
    out = rules.budget_events({"claude": {"five_hour_pct": 60.0}}, hud, {key: {"last_notified_at": _iso(NOW)}})
    assert out == []


# ---------------------------------------------------------------- park_refused / handoff (no AgentState transition)


def test_park_refused_notification():
    refusals = [{"session_id": "s9", "at": _iso(NOW), "parked_count": 6, "max_parked": 6}]
    out = rules.park_refused_notifications(refusals, {})
    assert len(out) == 1 and out[0].title == "Parking refused"
    assert "s9" in out[0].detail and "s9" not in out[0].body  # the session id is deck-only, not pushed


def test_park_refused_dedupe():
    at = _iso(NOW)
    refusals = [{"session_id": "s9", "at": at, "parked_count": 6, "max_parked": 6}]
    key = f"park_refused:s9:{at}"
    out = rules.park_refused_notifications(refusals, {key: {"last_notified_at": at}})
    assert out == []


class _FakeEvent:
    def __init__(self, **kw):
        self.event = "handoff"
        self.session_id = kw.get("session_id")
        self.ts = kw.get("ts", "")
        self.project = kw.get("project")
        self.extra = kw.get("extra", {})


def test_handoff_notification():
    e = _FakeEvent(session_id="s1", ts=_iso(NOW), project="loom-os", extra={"to_provider": "codex"})
    out = rules.handoff_notifications([e], {})
    assert len(out) == 1 and out[0].title == "Handed to codex" and out[0].tier == rules.TIER_SILENT


# ---------------------------------------------------------------- quiet hours


def test_quiet_hours_suppresses_caution_but_allows_warning():
    # Naive on purpose: `is_quiet_hours` reads wall-clock local time via `now.astimezone()`, and
    # a naive datetime is presumed already-local (stdlib docs), so this is deterministic
    # regardless of the machine's real timezone -- a tz-aware UTC stamp would drift with it.
    midnight = datetime(2026, 9, 2, 23, 30, 0)  # inside default 23:00-08:00
    assert rules.allowed_during_quiet_hours(rules.TIER_CAUTION, SETTINGS.quiet_hours, midnight) is False
    assert rules.allowed_during_quiet_hours(rules.TIER_WARNING, SETTINGS.quiet_hours, midnight) is True


def test_not_quiet_hours_allows_everything():
    noon = datetime(2026, 9, 2, 12, 0, 0)
    assert rules.allowed_during_quiet_hours(rules.TIER_CAUTION, SETTINGS.quiet_hours, noon) is True


# ---------------------------------------------------------------- transitions()


def test_transitions_detects_a_status_change():
    row = AgentState(session_id="s1", provider="claude", status=AgentStatus.BLOCKED_PERMISSION,
                     project="loom-os", last_event_ts=_iso(NOW))
    out = rules.transitions({"s1": "working"}, [row], now=NOW)
    assert len(out) == 1 and out[0].to_status == "blocked-permission" and out[0].from_status == "working"


def test_transitions_ignores_an_unchanged_status():
    row = AgentState(session_id="s1", provider="claude", status=AgentStatus.WORKING, last_event_ts=_iso(NOW))
    out = rules.transitions({"s1": "working"}, [row], now=NOW)
    assert out == []


def test_transitions_treats_an_unseen_session_as_from_none():
    row = AgentState(session_id="s2", provider="codex", status=AgentStatus.FAILED, last_event_ts=_iso(NOW))
    out = rules.transitions({}, [row], now=NOW)
    assert len(out) == 1 and out[0].from_status is None and out[0].to_status == "failed"


# ---------------------------------------------------------------- lock-screen text only
# (OpenClaw rule
# section 3/7: the payload that leaves the machine (ntfy/Telegram) or sits on the lock screen
# (toast) carries only the actor, the need, the project name and the window number -- never a
# tool command, a file path, prompt text, or a session id. `Notification.detail` is where that
# extra context goes instead (sent.jsonl / the deck only); `title`/`body` must never carry it.

_FORBIDDEN = ("/", "\\", "C:", "`")


def _assert_lock_screen_safe(n: rules.Notification) -> None:
    payload = f"{n.title} {n.body}"
    for bad in _FORBIDDEN:
        assert bad not in payload, f"{bad!r} leaked into pushed text: {payload!r}"


def test_failed_notification_never_pushes_the_log_path():
    """Fed a real Windows path on purpose -- this proves the field is actually stripped from the
    pushed text, not just absent because the fixture happened to be clean."""
    n = rules.decide(
        t("failed", exit_code=1, job_id="job-7",
          log_path="C:/Home/x/Documents/Projects/project_lanterns/state/dispatch/job-7.log"),
        AWAY, SETTINGS, {}, now=NOW,
    )
    assert n is not None
    _assert_lock_screen_safe(n)
    assert "job-7.log" in n.detail  # the path is kept, just not in the pushed title/body


def test_park_refused_never_pushes_the_session_id():
    refusals = [{"session_id": "s9-really-quite-specific", "at": _iso(NOW), "parked_count": 6, "max_parked": 6}]
    out = rules.park_refused_notifications(refusals, {})
    assert len(out) == 1
    _assert_lock_screen_safe(out[0])
    assert "s9-really-quite-specific" in out[0].detail


def test_lock_screen_sweep_across_every_rule_row():
    """Every `Notification` this module can produce, title+body checked against the forbidden
    set -- the mechanical backstop for the whole rule, not just the two paths above."""
    notifications = [
        rules.decide(t("blocked-permission", age_seconds=90), AWAY, SETTINGS, {}, now=NOW),
        rules.decide(
            t("waiting-input", age_seconds=16 * 60), AWAY, SETTINGS,
            {"s1:waiting-input": {"last_notified_at": _iso(NOW - timedelta(minutes=16)), "nudges_sent": 0}},
            now=NOW,
        ),
        rules.decide(t("failed", exit_code=1, job_id="job-1"), AWAY, SETTINGS, {}, now=NOW),
        rules.decide(t("done", tracker_id="row-1", summary="Fix Homebase task card overflow"), AWAY, SETTINGS, {}, now=NOW),
        rules.decide(t("parked", resume_after=_iso(NOW + timedelta(hours=1))), AWAY, SETTINGS, {}, now=NOW),
        rules.decide(t("working", from_status="parked"), AWAY, SETTINGS, {}, now=NOW),
        rules.decide(t("resume-failed"), AWAY, SETTINGS, {}, now=NOW),
    ]
    notifications += rules.budget_events(
        {"claude": {"five_hour_pct": 60.0}},
        {"claude": {"five_hour_pct": 92.0, "five_hour_resets_at": _iso(NOW + timedelta(hours=2))}},
        {},
    )
    notifications += rules.park_refused_notifications(
        [{"session_id": "s9", "at": _iso(NOW), "parked_count": 6, "max_parked": 6}], {}
    )
    notifications += rules.handoff_notifications(
        [_FakeEvent(session_id="s1", ts=_iso(NOW), project="loom-os", extra={"to_provider": "codex"})], {}
    )
    assert all(n is not None for n in notifications)
    for n in notifications:
        _assert_lock_screen_safe(n)
