"""The account usage reader (`pantheon/hud/account.py`): (decision
board) lets the collector read the account's own usage numbers from Anthropic as a second,
less-trusted source behind the status line. Every test here uses a fake login file and a fake HTTP
layer; `tests/conftest.py` makes sure nothing reaches the network or the real login."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from pantheon import config
from pantheon.hud import account, collector, sources
from pantheon.hud import app as hud_app
from pantheon.models import ProviderUsage

TOKEN = "sk-ant-oat01-FAKE-TOKEN-never-to-be-seen-0123456789"
START = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def payload(five=42.0, week=17.0, five_reset=None, week_reset=None, **extra):
    body = {
        "five_hour": {"utilization": five,
                      "resets_at": five_reset or (START + timedelta(hours=3)).isoformat()},
        "seven_day": {"utilization": week,
                      "resets_at": week_reset or (START + timedelta(days=4)).isoformat()},
        "seven_day_opus": {"utilization": 8.0, "resets_at": (START + timedelta(days=4)).isoformat()},
        "seven_day_sonnet": None,
    }
    body.update(extra)
    return json.dumps(body).encode()


class FakeHttp:
    """Answers from a list of (status, headers, body); records every request it saw."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        status, hdrs, body = self.answers.pop(0) if self.answers else (200, {}, payload())
        if isinstance(status, Exception):
            raise status
        return status, hdrs, body


@pytest.fixture
def cfg(tmp_path):
    c = config.Config(state_dir=str(tmp_path / "state"), claude_home=str(tmp_path / "claude"))
    config.ensure_state_dirs(c)
    return c


@pytest.fixture
def login(tmp_path):
    path = tmp_path / "login.json"

    def write(expires: datetime = START + timedelta(hours=8), token: str = TOKEN):
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token, "refreshToken": "rt-FAKE",
            "expiresAt": int(expires.timestamp() * 1000), "subscriptionType": "max"}}))
        return path

    return write


def now_at(minutes: float = 0) -> float:
    return (START + timedelta(minutes=minutes)).timestamp()


# --------------------------------------------------------------------------- parsing

def test_parse_reads_flat_windows_and_per_model_weeks():
    got = account.parse(json.loads(payload(five=42.5, week=17)))
    assert got["five_hour"] == {"pct": 42.5, "resets_at": iso(START + timedelta(hours=3))}
    assert got["seven_day"]["pct"] == 17.0
    assert got["models"] == {"Opus": {"pct": 8.0, "resets_at": iso(START + timedelta(days=4))}}


def test_parse_prefers_the_limits_list_and_names_scoped_models():
    body = {
        "five_hour": {"utilization": 10, "resets_at": "2026-09-27T15:00:00.412000+00:00"},
        "seven_day_opus": None,
        "limits": [
            {"kind": "session", "percent": 12, "is_active": True},
            {"kind": "weekly_all", "percent": 30, "resets_at": "2026-10-01T00:00:00Z"},
            {"kind": "weekly_scoped", "percent": 55, "resets_at": "2026-10-01T00:00:00Z",
             "scope": {"model": {"display_name": "Fable", "id": "claude-fable-5-1"}}},
            {"kind": "weekly_scoped", "percent": None},
        ],
    }
    got = account.parse(body)
    assert got["five_hour"] == {"pct": 12.0, "resets_at": "2026-09-27T15:00:00Z"}   # reset from the flat key
    assert got["seven_day"] == {"pct": 30.0, "resets_at": "2026-10-01T00:00:00Z"}
    assert got["models"] == {"Fable": {"pct": 55.0, "resets_at": "2026-10-01T00:00:00Z"}}


def test_parse_of_nonsense_is_empty_not_an_error():
    assert account.parse(["no"]) == {"five_hour": None, "seven_day": None, "models": {}}
    assert account.parse({"five_hour": {"utilization": "lots"}})["five_hour"] is None


# --------------------------------------------------------------------------- the login on disk

def test_read_token_ok_expired_missing_and_broken(tmp_path, login):
    assert account.read_token(login(), now=now_at()) == (TOKEN, "ok")
    token, reason = account.read_token(login(expires=START - timedelta(minutes=1)), now=now_at())
    assert token is None and "out of date" in reason and TOKEN not in reason
    assert account.read_token(tmp_path / "nope.json", now=now_at()) == (None, "no Claude login on this PC")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json " + TOKEN)
    token, reason = account.read_token(bad, now=now_at())
    assert token is None and TOKEN not in reason


def test_the_request_looks_like_claude_code(cfg, login, tmp_path):
    capture = tmp_path / "state" / "statusline" / "s1.json"
    capture.write_text(json.dumps({"version": "2.1.283"}))
    http = FakeHttp()
    account.poll(cfg, now=now_at(), http=http, credentials=login())
    (url, headers, timeout), = http.calls
    assert url == "https://api.anthropic.com/api/oauth/usage"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["User-Agent"] == "claude-code/2.1.283"
    assert timeout <= 15


def test_an_expired_login_makes_no_request(cfg, login):
    http = FakeHttp()
    state = account.poll(cfg, now=now_at(), http=http, credentials=login(expires=START))
    assert http.calls == []
    assert "out of date" in state["status"]
    assert state["next_at"] == now_at(5)


# --------------------------------------------------------------------------- schedule + back-off

def test_one_request_per_five_minutes_at_most(cfg, login):
    http = FakeHttp()
    path = login()
    for minute in (0, 1, 2, 4.9):
        account.poll(cfg, now=now_at(minute), http=http, credentials=path)
    assert len(http.calls) == 1
    state = account.poll(cfg, now=now_at(5), http=http, credentials=path)
    assert len(http.calls) == 2 and state["status"] == "ok"
    assert state["reading"]["five_hour"]["pct"] == 42.0
    assert state["reading"]["fetched_at"] == iso(START + timedelta(minutes=5))


def test_429_backs_off_exponentially_capped_and_success_resets(cfg, login):
    path = login(expires=START + timedelta(days=2))
    http = FakeHttp(*[(429, {"retry-after": "0"}, b"")] * 5, (200, {}, payload()))
    waits, t = [], 0.0
    for _ in range(5):
        state = account.poll(cfg, now=now_at(t), http=http, credentials=path)
        waits.append(round((state["next_at"] - now_at(t)) / 60))
        assert "429" in state["status"]
        # a poll before the wait is over makes no request
        account.poll(cfg, now=state["next_at"] - 1, http=http, credentials=path)
        t = (state["next_at"] - START.timestamp()) / 60
    assert waits == [10, 20, 40, 60, 60]
    assert len(http.calls) == 5
    state = account.poll(cfg, now=now_at(t), http=http, credentials=path)
    assert state["status"] == "ok" and state["backoff_s"] == 0
    assert state["next_at"] == now_at(t) + 300


def test_retry_after_is_honoured_when_longer(cfg, login):
    http = FakeHttp((429, {"retry-after": "5400"}, b""))
    state = account.poll(cfg, now=now_at(), http=http, credentials=login())
    assert state["next_at"] == now_at(90)


@pytest.mark.parametrize("answer", [
    (401, {}, b""), (500, {}, b""), (0, {}, b""), (200, {}, b"<html>not json"),
    (200, {}, b"{}"), (RuntimeError(f"boom Bearer {TOKEN}"), {}, b""),
])
def test_every_failure_backs_off_and_keeps_the_last_good_reading(cfg, login, answer):
    path = login()
    http = FakeHttp((200, {}, payload(five=33)), answer)
    account.poll(cfg, now=now_at(0), http=http, credentials=path)
    state = account.poll(cfg, now=now_at(5), http=http, credentials=path)
    assert state["status"] != "ok"
    assert state["next_at"] >= now_at(15)
    assert state["reading"]["five_hour"]["pct"] == 33.0
    assert len(http.calls) == 2


def test_a_second_collector_mid_call_does_not_call_too(cfg, login):
    lock = cfg.hud_file.with_name(account.LOCK_FILE)
    lock.write_text("")
    http = FakeHttp()
    account.poll(cfg, now=now_at(), http=http, credentials=login())
    assert http.calls == []


# --------------------------------------------------------------------------- precedence

def _live(pct, as_of, reset):
    u = ProviderUsage(provider="claude", source="statusline")
    u.five_hour_pct, u.five_hour_as_of, u.five_hour_resets_at = pct, iso(as_of), iso(reset)
    u.seven_day_as_of = None
    return u


def _state(five=50.0, fetched=START, five_reset=None):
    return {"status": "ok", "next_at": fetched.timestamp() + 300, "reading": {
        "fetched_at": iso(fetched),
        "five_hour": {"pct": five, "resets_at": iso(five_reset or START + timedelta(hours=2))},
        "seven_day": {"pct": 20.0, "resets_at": iso(START + timedelta(days=3))},
        "models": {"Fable": {"pct": 61.0, "resets_at": iso(START + timedelta(days=3))}},
    }}


def test_a_newer_status_line_wins():
    usage = _live(44.0, START + timedelta(minutes=2), START + timedelta(hours=2))
    account.apply(usage, _state(fetched=START), now_at(3))
    assert usage.five_hour_pct == 44.0 and usage.five_hour_source == "statusline"
    assert usage.source == "statusline"
    assert usage.seven_day_pct == 20.0 and usage.seven_day_source == "account"   # it had none
    assert usage.seven_day_models == {"Fable": {"pct": 61.0, "resets_at": iso(START + timedelta(days=3))}}


def test_a_newer_account_reading_wins_and_says_so():
    usage = _live(44.0, START - timedelta(minutes=40), START + timedelta(hours=2))
    account.apply(usage, _state(five=51.0, fetched=START), now_at(1))
    assert usage.five_hour_pct == 51.0 and usage.five_hour_source == "account"
    assert usage.five_hour_as_of == iso(START)
    assert usage.source == "account" and usage.reported_at == iso(START)


def test_an_old_or_reset_account_reading_is_not_used():
    usage = _live(44.0, START - timedelta(hours=8), START + timedelta(hours=2))
    account.apply(usage, _state(fetched=START - timedelta(hours=7)), now_at())
    assert usage.five_hour_pct == 44.0 and usage.seven_day_pct is None
    usage = ProviderUsage(provider="claude", source="unknown")
    account.apply(usage, _state(fetched=START, five_reset=START + timedelta(minutes=1)), now_at(2))
    assert usage.five_hour_pct is None and usage.five_hour_source is None
    assert usage.seven_day_pct == 20.0 and usage.source == "account"


# --------------------------------------------------------------------------- the collector

def _quiet_sources(monkeypatch, live=None):
    monkeypatch.setattr(sources, "claude_from_statusline",
                        lambda *a, **k: live() if live else ProviderUsage(provider="claude", source="unknown"))
    monkeypatch.setattr(sources, "codex_rate_limits", lambda *a, **k: ProviderUsage(provider="codex"))
    monkeypatch.setattr(sources, "claude_from_ccusage", lambda *a, **k: ProviderUsage(provider="claude"))
    monkeypatch.setattr(sources, "codex_from_ccusage", lambda *a, **k: ProviderUsage(provider="codex"))


def _wire(monkeypatch, cfg, login, http):
    path = login(expires=datetime.now(timezone.utc) + timedelta(hours=8))
    monkeypatch.setattr(account, "default_credentials_path", lambda c: path)
    monkeypatch.setattr(account, "http_get", http)


def test_only_the_collector_pass_reads_the_account(cfg, login, monkeypatch):
    _quiet_sources(monkeypatch)
    # The collector pass reads the real clock, so the resets must be in the real future -- the
    # START-based defaults went stale the same afternoon.
    now = datetime.now(timezone.utc)
    body = payload(five_reset=(now + timedelta(hours=3)).isoformat(),
                   week_reset=(now + timedelta(days=4)).isoformat(),
                   seven_day_opus={"utilization": 8.0, "resets_at": (now + timedelta(days=4)).isoformat()})
    http = FakeHttp(*[(200, {}, body)] * 3)
    _wire(monkeypatch, cfg, login, http)
    sources.collect(cfg)                                  # full pass from anyone else (the governor)
    sources.collect(cfg, slow=False)                      # the readers' quick pass
    assert http.calls == []
    picture = collector.collect_pass(cfg)
    assert len(http.calls) == 1
    claude = picture["claude"]
    assert claude["five_hour_pct"] == 42.0 and claude["five_hour_source"] == "account"
    assert claude["source"] == "account" and claude["seven_day_models"]["Opus"]["pct"] == 8.0
    # every later pass inside five minutes carries the reading without asking again
    again = sources.collect(cfg, slow=False)
    assert again["claude"]["five_hour_pct"] == 42.0
    collector.collect_pass(cfg)
    assert len(http.calls) == 1


def test_the_off_switch_is_todays_behaviour_exactly(cfg, login, monkeypatch, tmp_path):
    toml = tmp_path / "pantheon.toml"
    toml.write_text(f'state_dir = "{cfg.state_dir}"\n[usage]\naccount_read = false\n')
    off = config.load(toml)
    assert off.usage_settings().account_read is False
    assert config.Config().usage_settings().account_read is True

    now = datetime.now(timezone.utc)
    live = lambda: _live(30.0, now, now + timedelta(hours=2))   # noqa: E731
    _quiet_sources(monkeypatch, live)
    http = FakeHttp()
    _wire(monkeypatch, off, login, http)
    picture = collector.collect_pass(off)
    assert http.calls == []
    assert not account.state_path(off).exists()
    for key in ("five_hour_source", "seven_day_source", "seven_day_models", "account_status"):
        assert key not in picture["claude"]
    assert picture["claude"]["source"] == "statusline" and picture["claude"]["five_hour_pct"] == 30.0


def test_the_token_never_reaches_logs_state_or_hud_json(cfg, login, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _quiet_sources(monkeypatch)
    answers = [(200, {}, payload()), (429, {}, b""), (RuntimeError(f"bad Bearer {TOKEN}"), {}, b""),
               (401, {}, TOKEN.encode())]
    http = FakeHttp(*answers)
    _wire(monkeypatch, cfg, login, http)
    for _ in answers:
        collector.collect_pass(cfg)
        state = account.load_state(cfg)
        state["next_at"] = 0                              # let the next pass call at once
        account.state_path(cfg).write_text(json.dumps(state))
    assert len(http.calls) == len(answers)
    written = "".join(p.read_text(encoding="utf-8", errors="replace")
                      for p in cfg.hud_file.parent.rglob("*") if p.is_file())
    assert TOKEN not in written
    assert TOKEN not in caplog.text
    assert TOKEN not in account.scrub(f"Authorization: Bearer {TOKEN}", None)


# --------------------------------------------------------------------------- the card's words

def test_the_card_says_where_the_number_came_from_only_when_it_is_the_account(cfg):
    two_min_ago = iso(datetime.now(timezone.utc) - timedelta(minutes=2))
    usage = {"provider": "claude", "source": "account", "five_hour_pct": 51.0,
             "five_hour_as_of": two_min_ago, "seven_day_pct": 20.0, "seven_day_as_of": two_min_ago}
    assert hud_app._source_tail(usage) == "from your account 2m ago"
    block = "\n".join(hud_app.render_block(usage, cfg, width=100)) if isinstance(
        hud_app.render_block(usage, cfg, width=100), list) else hud_app.render_block(usage, cfg, width=100)
    assert "from your account" in block and "2m ago" in block
    live = dict(usage, source="statusline")
    assert hud_app._source_tail(live) is None
    live_block = hud_app.render_block(live, cfg, width=100)
    assert "from your account" not in (live_block if isinstance(live_block, str) else "\n".join(live_block))
