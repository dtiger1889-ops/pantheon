"""Telegram into the Assistant (pantheon/assistant_telegram.py). Every Bot API call goes to a fake;
no socket is opened, no token is read, and tmux is the conftest's "not available" stub."""
from __future__ import annotations

import json
import socket
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from pantheon import assistant as assistant_mod
from pantheon import assistant_telegram as tg
from pantheon import config as config_mod
from pantheon import events as events_mod

CHAT = "111222333"
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path, **link):
    return config_mod.Config(state_dir=str(tmp_path / "state"), projects_root=str(tmp_path),
                             assistant={"telegram": {"enabled": True, "chat_id": CHAT, **link}})


class FakeApi:
    def __init__(self, batches=()):
        self.batches = list(batches)
        self.polls = []
        self.sent = []

    def get_updates(self, offset, poll_seconds):
        self.polls.append((offset, poll_seconds))
        return self.batches.pop(0) if self.batches else []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakePort:
    def __init__(self, outcome=("sent", ""), states=(("done", "hello from the Assistant"),), sid="s-1"):
        self.outcome, self.states, self.sid = outcome, list(states), sid
        self.delivered = []

    def deliver(self, text):
        self.delivered.append(text)
        return self.outcome

    def session_id(self):
        return self.sid

    def check(self, since):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def _update(uid, chat=CHAT, text="what is on today?", when=NOW):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text,
                                           "date": int(when.timestamp())}}


def _poller(cfg, api, port, clock=None):
    clock = clock or Clock()
    return tg.Poller(cfg, tg.Link.from_config(cfg), api, port, clock=clock, sleep=clock.sleep,
                     now=lambda: NOW)


# --------------------------------------------------------------------------- who may write


def test_a_stranger_is_ignored_silently(tmp_path):
    api, port = FakeApi([[_update(5, chat="999")]]), FakePort()
    assert _poller(_cfg(tmp_path), api, port).run(max_cycles=1) == "cycles"
    assert port.delivered == [] and api.sent == []


def test_no_chat_id_configured_means_nobody_gets_in(tmp_path):
    cfg = _cfg(tmp_path, chat_id="")
    port = FakePort()
    assert _poller(cfg, FakeApi(), port).handle(_update(1)) == "denied"
    assert port.delivered == []


def test_chat_id_and_credential_fall_back_to_the_notifier(tmp_path):
    cfg = config_mod.Config(state_dir=str(tmp_path), assistant={"telegram": {"enabled": True}},
                            notify={"telegram": {"chat_id": CHAT, "credential_target": "tg-bot"}})
    link = tg.Link.from_config(cfg)
    assert (link.chat_id, link.credential_target) == (CHAT, "tg-bot")


# --------------------------------------------------------------------------- the round trip


def test_round_trip_message_in_reply_out(tmp_path):
    cfg = _cfg(tmp_path)
    api = FakeApi([[_update(41)]])
    port = FakePort(states=[("working", ""), ("done", "Dentist at 3, nothing else.")])
    p = _poller(cfg, api, port)
    assert p.run(max_cycles=1) == "cycles"
    assert port.delivered == ["(sent from Telegram) what is on today?"]
    assert api.sent == [(CHAT, "Dentist at 3, nothing else.")]
    assert p.offset == 42
    assert json.loads((tmp_path / "state" / tg.STATE_FILE).read_text())["offset"] == 42
    assert _poller(cfg, FakeApi(), port).offset == 42      # a restart does not deliver it twice


def test_busy_assistant_is_not_typed_into_and_the_user_is_told(tmp_path):
    api, port = FakeApi(), FakePort(outcome=("busy", tg.MSG_BUSY))
    assert _poller(_cfg(tmp_path), api, port).handle(_update(1)) == "busy"
    assert api.sent == [(CHAT, tg.MSG_BUSY)]


def test_a_question_on_its_screen_is_reported_once(tmp_path):
    api = FakeApi()
    port = FakePort(states=[("needs_you", ""), ("needs_you", ""), ("done", "ok")])
    assert _poller(_cfg(tmp_path), api, port).handle(_update(1)) == "replied"
    assert api.sent == [(CHAT, tg.MSG_NEEDS_YOU), (CHAT, "ok")]


def test_an_old_backlog_message_is_not_passed_on(tmp_path):
    api, port = FakeApi(), FakePort()
    stale = _update(1, when=NOW - timedelta(hours=2))
    assert _poller(_cfg(tmp_path), api, port).handle(stale) == "too_old"
    assert port.delivered == [] and "not passed on" in api.sent[0][1]


def test_a_message_that_queued_during_a_long_reply_wait_is_not_stale(tmp_path):
    clock_now = [NOW]
    api, port = FakeApi(), FakePort()
    p = tg.Poller(_cfg(tmp_path), tg.Link.from_config(_cfg(tmp_path)), api, port,
                  now=lambda: clock_now[0], sleep=lambda s: None)
    clock_now[0] = NOW + timedelta(minutes=30)
    assert p.handle(_update(2, when=NOW + timedelta(minutes=5))) == "replied"


# --------------------------------------------------------------------------- off switch


def test_off_switch_stops_before_any_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("PANTHEON_ASSISTANT", "off")
    api = FakeApi([[_update(1)]])
    assert _poller(_cfg(tmp_path), api, FakePort()).run(max_cycles=5) == "off"
    assert api.polls == []


def test_off_switch_thrown_while_running_stops_the_next_cycle(tmp_path, monkeypatch):
    api = FakeApi()
    p = _poller(_cfg(tmp_path), api, FakePort())
    assert p.run(max_cycles=1) == "cycles" and len(api.polls) == 1
    monkeypatch.setenv("PANTHEON_ASSISTANT", "off")
    assert p.run(max_cycles=5) == "off" and len(api.polls) == 1


def test_main_does_nothing_when_telegram_is_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_mod, "load", lambda: config_mod.Config(state_dir=str(tmp_path)))
    assert tg.main([]) == 0
    assert "off" in capsys.readouterr().out


# --------------------------------------------------------------------------- deadlines


class FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_every_get_updates_call_has_a_finite_deadline():
    seen = []

    def opener(req, timeout=None):
        seen.append((timeout, json.loads(req.data)))
        return FakeResponse({"ok": True, "result": [{"update_id": 7}]})

    api = tg.BotApi(lambda: "TOKEN-123", opener=opener)
    assert api.get_updates(0, 25) == [{"update_id": 7}]
    timeout, payload = seen[0]
    assert payload["timeout"] == 25
    assert timeout == 25 + tg.HTTP_MARGIN_SECONDS


def test_a_hung_poll_times_out_without_leaking_the_token_and_the_loop_backs_off(tmp_path):
    def opener(req, timeout=None):
        raise socket.timeout("timed out")

    api = tg.BotApi(lambda: "TOKEN-123", opener=opener)
    with pytest.raises(tg.TelegramError) as err:
        api.get_updates(0, 25)
    assert "TOKEN" not in str(err.value)
    clock = Clock()
    p = tg.Poller(_cfg(tmp_path), tg.Link.from_config(_cfg(tmp_path)), api, FakePort(),
                  clock=clock, sleep=clock.sleep)
    assert p.run(max_cycles=3) == "cycles"
    assert clock.slept == [5.0, 10.0, 20.0]


def test_a_401_rereads_the_token_next_time():
    reads = []

    def read():
        reads.append(1)
        return "TOKEN-123"

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    api = tg.BotApi(read, opener=opener)
    for _ in range(2):
        with pytest.raises(tg.TelegramError) as err:
            api.send_message(CHAT, "x")
        assert "TOKEN" not in str(err.value)
    assert len(reads) == 2


def test_the_reply_wait_ends_at_its_deadline(tmp_path):
    cfg = _cfg(tmp_path, reply_wait_seconds=60)
    api, clock = FakeApi(), Clock()
    port = FakePort(states=[("working", "")])
    assert _poller(cfg, api, port, clock).handle(_update(1)) == "timed_out"
    assert clock.t >= 60 and clock.t < 63
    assert api.sent == [(CHAT, tg.MSG_TIMEOUT.format(minutes=1))]


# --------------------------------------------------------------------------- the real port


def _iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def test_port_reads_the_reply_after_the_stop_event(tmp_path):
    cfg = _cfg(tmp_path)
    transcript = tmp_path / "t.jsonl"
    assistant_mod.write_state(cfg, {"session_id": "s-1", "pane_id": "%9", "started_at": _iso(NOW - timedelta(hours=1)),
                                    "transcript_path": transcript.as_posix(), "folder": str(tmp_path)})
    sent = NOW

    def rec(dt, content):
        return json.dumps({"type": "assistant", "timestamp": _iso(sent + timedelta(seconds=dt)),
                           "message": {"content": content}})

    transcript.write_text("\n".join([
        json.dumps({"type": "assistant", "timestamp": _iso(sent - timedelta(minutes=5)),
                    "message": {"content": [{"type": "text", "text": "an older answer"}]}}),
        rec(1, [{"type": "text", "text": "Let me check."}, {"type": "tool_use", "name": "Read"}]),
        rec(3, [{"type": "text", "text": "Dentist at 3."}]),
    ]), encoding="utf-8")
    port = tg.AssistantPort(cfg)
    assert port.check(sent) == ("working", "")
    events_mod.append_event(cfg.events_file, {"ts": _iso(sent + timedelta(seconds=4)), "event": "Stop",
                                              "source": "claude", "session_id": "s-1"})
    assert port.check(sent) == ("done", "Dentist at 3.")


def test_a_late_stop_from_the_turn_before_does_not_end_the_wait(tmp_path):
    cfg = _cfg(tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    assistant_mod.write_state(cfg, {"session_id": "s-1", "pane_id": "%9", "started_at": _iso(NOW - timedelta(hours=1)),
                                    "transcript_path": transcript.as_posix()})
    events_mod.append_event(cfg.events_file, {"ts": _iso(NOW + timedelta(seconds=1)), "event": "Stop",
                                              "source": "claude", "session_id": "s-1"})
    assert tg.AssistantPort(cfg).check(NOW) == ("working", "")


def test_port_never_types_into_a_busy_assistant(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(assistant_mod, "locate",
                        lambda cfg, tmux=None: assistant_mod.Place("%9", 5, "ASSISTANT", "node"))
    calls = []
    monkeypatch.setattr(tg.tmuxctl, "run", lambda *a, **k: calls.append(a))
    port = tg.AssistantPort(cfg, capture=lambda target, tmux=None: "⠋ Thinking…\n", sleep=lambda s: None)
    assert port.deliver("hi")[0] == "busy"
    assert calls == []


def test_port_says_not_running_when_there_is_no_assistant(tmp_path):
    assert tg.AssistantPort(_cfg(tmp_path)).deliver("hi") == ("not_running", tg.MSG_NOT_RUNNING)
