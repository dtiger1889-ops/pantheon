"""Voice into the Assistant (pantheon/voice_endpoint.py). Servers bind 127.0.0.1 on a free port
and stop at the end of each test; the Assistant is always a fake port, so nothing reaches tmux."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from datetime import datetime, timezone

import pytest

from pantheon import config as config_mod
from pantheon import voice_endpoint as ve

TOKEN = "test-token-0123456789abcdef"
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


class FakePort:
    def __init__(self, outcome=("sent", ""), states=(("done", "Dentist at 3, nothing else."),), sid="s-1",
                 gate: threading.Event | None = None):
        self.outcome, self.states, self.sid, self.gate = outcome, list(states), sid, gate
        self.delivered = []
        self.checking = threading.Event()

    def deliver(self, text):
        self.delivered.append(text)
        return self.outcome

    def session_id(self):
        return self.sid

    def check(self, since):
        self.checking.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _cfg(tmp_path, **voice):
    return config_mod.Config(state_dir=str(tmp_path / "state"), projects_root=str(tmp_path),
                             voice={"enabled": True, **voice})


def _voice(tmp_path, assistant, **voice):
    cfg = _cfg(tmp_path, **voice)
    clock = Clock()
    return ve.Voice(cfg, ve.Settings.from_config(cfg), assistant, clock=clock, sleep=clock.sleep,
                    now=lambda: NOW, check_every=1.0)


@pytest.fixture
def serve(tmp_path):
    started = []

    def _start(assistant=None, **voice):
        voice.setdefault("port", 0)
        voice_obj = _voice(tmp_path, assistant or FakePort(), **voice)
        server = ve.make_server(voice_obj, voice_obj.settings, TOKEN)
        t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        started.append((server, t))
        return server.server_address[1], voice_obj

    yield _start
    for server, t in started:
        server.shutdown()
        server.server_close()
        t.join(5)


def _request(port, method="POST", path="/v1/chat/completions", body=None, token=TOKEN, raw=None,
             headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    out = resp.status, resp.getheader("Content-Type") or "", resp.read().decode()
    conn.close()
    return out


def _chat(text="what is on today?", **extra):
    return {"model": "pantheon-assistant", "max_tokens": 3000,
            "messages": [{"role": "system", "content": "You are a voice assistant."},
                         {"role": "user", "content": "an earlier question"},
                         {"role": "assistant", "content": "an earlier answer"},
                         {"role": "user", "content": text}], **extra}


def _content(body):
    return json.loads(body)["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- off by default


def test_voice_is_off_unless_switched_on():
    assert ve.Settings.from_config(config_mod.Config()).enabled is False
    assert ve.Settings.from_config(config_mod.Config(voice={"enabled": "false"})).enabled is False


def test_the_example_toml_ships_voice_off():
    # A user's own pantheon.toml may switch voice on; the example never does.
    root = ve.PROJECT_ROOT
    example = ve.Settings.from_config(config_mod.load(root / "pantheon.example.toml"))
    assert example.enabled is False and example.allow == ve.DEFAULT_ALLOW and example.token_file == ""


def test_the_launcher_opens_voice_only_with_voice_and_the_assistant_on(monkeypatch):
    monkeypatch.delenv("PANTHEON_ASSISTANT", raising=False)
    assert ve.should_autostart(config_mod.Config()) is False
    assert ve.should_autostart(config_mod.Config(voice={"enabled": True})) is True
    monkeypatch.setenv("PANTHEON_ASSISTANT", "off")   # what the launcher test lane's decks run with
    assert ve.should_autostart(config_mod.Config(voice={"enabled": True})) is False


def test_a_voice_table_that_does_not_parse_still_opens_the_window_to_show_why(monkeypatch):
    monkeypatch.delenv("PANTHEON_ASSISTANT", raising=False)
    assert ve.should_autostart(config_mod.Config(voice={"enabled": True, "port": 70000})) is True


def test_main_does_not_listen_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_mod, "load", lambda: config_mod.Config(state_dir=str(tmp_path)))
    monkeypatch.setattr(ve, "make_server", lambda *a, **k: pytest.fail("must not start a server"))
    assert ve.main([]) == 0
    assert "off" in capsys.readouterr().out


def test_main_refuses_to_start_without_a_usable_token(tmp_path, monkeypatch, capsys):
    cfg = config_mod.Config(state_dir=str(tmp_path), voice={"enabled": True,
                                                             "token_file": str(tmp_path / "missing")})
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    monkeypatch.setattr(ve, "make_server", lambda *a, **k: pytest.fail("must not start a server"))
    assert ve.main([]) == 2
    assert "token" in capsys.readouterr().out


def test_main_check_reports_ok_without_listening(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "voice_token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    cfg = config_mod.Config(state_dir=str(tmp_path), voice={"enabled": True, "token_file": str(token_file)})
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    monkeypatch.setattr(ve, "make_server", lambda *a, **k: pytest.fail("must not start a server"))
    assert ve.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and TOKEN not in out


# --------------------------------------------------------------------------- the token file


def test_token_file_rules(tmp_path):
    good = tmp_path / "tok"
    good.write_text(f"  {TOKEN}\n", encoding="utf-8")
    assert ve.read_token(ve.Settings(token_file=str(good)), project_root=tmp_path / "repo") == TOKEN
    with pytest.raises(ve.TokenError):
        ve.read_token(ve.Settings(token_file=""))
    with pytest.raises(ve.TokenError):
        ve.read_token(ve.Settings(token_file=str(tmp_path / "nope")), project_root=tmp_path / "repo")
    short = tmp_path / "short"
    short.write_text("abc", encoding="utf-8")
    with pytest.raises(ve.TokenError) as err:
        ve.read_token(ve.Settings(token_file=str(short)), project_root=tmp_path / "repo")
    assert "abc" not in str(err.value)
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / "voice_token"
    inside.write_text(TOKEN, encoding="utf-8")
    with pytest.raises(ve.TokenError) as err:
        ve.read_token(ve.Settings(token_file=str(inside)), project_root=repo)
    assert "inside" in str(err.value) and TOKEN not in str(err.value)


# --------------------------------------------------------------------------- who may call


def test_missing_token_is_refused_and_nothing_is_typed(serve):
    port = FakePort()
    p, _ = serve(port)
    status, _, body = _request(p, body=_chat(), token=None)
    assert status == 401 and "API key" in body
    assert port.delivered == []


def test_wrong_token_is_refused(serve):
    port = FakePort()
    p, _ = serve(port)
    assert _request(p, body=_chat(), token="wrong-token-0123456789")[0] == 401
    assert _request(p, method="GET", path="/v1/models", token="nope")[0] == 401
    assert port.delivered == []


def test_a_caller_outside_the_allowlist_gets_no_answer(serve):
    port = FakePort()
    p, _ = serve(port, allow=["192.168.0.0/16"])      # 127.0.0.1 is not in it
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError, OSError)):
        _request(p, body=_chat())
    assert port.delivered == []


def test_slow_callers_cannot_pile_up_connections(serve, monkeypatch):
    monkeypatch.setattr(ve, "MAX_OPEN_CONNECTIONS", 2)
    port = FakePort()
    p, _ = serve(port)
    held = [socket.create_connection(("127.0.0.1", p), timeout=5) for _ in range(2)]
    try:
        for s in held:
            s.sendall(b"POST /v1/chat/completions HTTP/1.0\r\n")   # half a request, then silence
        time.sleep(0.3)
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError, OSError)):
            _request(p, body=_chat())
    finally:
        for s in held:
            s.close()
    time.sleep(0.3)
    assert _request(p, body=_chat())[0] == 200      # the slots come back once those close


@pytest.mark.parametrize("addr, ok", [
    ("127.0.0.1", True), ("::1", True), ("192.168.1.50", True), ("10.1.2.3", True),
    ("172.16.0.9", True), ("172.32.0.1", False), ("100.101.102.103", True), ("100.128.0.1", False),
    ("fd7a:115c:a1e0::1", True), ("::ffff:192.168.1.5", True), ("::ffff:8.8.8.8", False),
    ("8.8.8.8", False), ("2001:4860::8888", False), ("not an address", False), ("", False),
])
def test_default_allowlist(addr, ok):
    assert ve.ip_allowed(addr, ve.Settings().networks) is ok


def test_a_bad_allowlist_entry_stops_startup():
    with pytest.raises(ValueError):
        ve.Settings.from_config(config_mod.Config(voice={"allow": ["192.168.1.0/24", "everyone"]}))
    with pytest.raises(ValueError):
        ve.Settings.from_config(config_mod.Config(voice={"allow": []}))


def test_reply_wait_is_capped_under_the_client_timeout():
    s = ve.Settings.from_config(config_mod.Config(voice={"reply_wait_seconds": 3600}))
    assert s.reply_wait_seconds == ve.MAX_WAIT_SECONDS < 600


# --------------------------------------------------------------------------- the round trip


def test_models_lists_the_assistant(serve):
    p, _ = serve()
    status, _, body = _request(p, method="GET", path="/v1/models")
    assert status == 200 and json.loads(body)["data"][0]["id"] == ve.MODEL_ID


def test_happy_path_types_the_last_user_message_and_returns_the_reply(serve):
    port = FakePort(states=[("working", ""), ("done", "**Dentist** at 3, nothing else.")])
    p, voice = serve(port)
    status, ctype, body = _request(p, body=_chat())
    assert status == 200 and ctype.startswith("application/json")
    assert _content(body) == "Dentist at 3, nothing else."
    assert port.delivered == ["(said out loud to a voice puck; answer in a few short spoken sentences) "
                              "what is on today?"]


def test_streaming_request_gets_one_chunk_and_done(serve):
    p, _ = serve(FakePort())
    status, ctype, body = _request(p, body=_chat(stream=True))
    assert status == 200 and ctype.startswith("text/event-stream")
    events = [line[len("data: "):] for line in body.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    first, last = json.loads(events[0]), json.loads(events[1])
    assert first["choices"][0]["delta"]["content"] == "Dentist at 3, nothing else."
    assert last["choices"][0]["finish_reason"] == "stop"


def test_home_assistant_setup_probe_is_answered_without_typing(serve):
    port = FakePort()
    p, _ = serve(port)
    probe = {"model": "pantheon-assistant", "max_tokens": 3,
             "messages": [{"role": "system", "content": "You are a helpful assistant."},
                          {"role": "user", "content": "What is the capital of France?"}],
             "tools": [{"type": "function", "function": {"name": "test_function", "description": "Test function.",
                                                         "parameters": {"type": "object", "properties": {}}}}]}
    for stream in (False, True):
        assert _request(p, body={**probe, "stream": stream})[0] == 200
    assert port.delivered == []


def test_timeout_is_spoken_with_a_200(serve):
    port = FakePort(states=[("working", "")])
    p, voice = serve(port, reply_wait_seconds=30)
    status, _, body = _request(p, body=_chat())
    assert status == 200 and _content(body) == ve.SAY_TIMEOUT
    assert 30 <= voice.clock.t < 32


def test_a_second_request_while_one_is_waiting_gets_the_busy_reply(serve):
    gate = threading.Event()
    port = FakePort(gate=gate)
    p, _ = serve(port)
    first = {}
    t = threading.Thread(target=lambda: first.update(r=_request(p, body=_chat("first"))))
    t.start()
    assert port.checking.wait(10)
    status, _, body = _request(p, body=_chat("second"))
    assert status == 200 and _content(body) == ve.SAY_STILL_WORKING
    gate.set()
    t.join(10)
    assert _content(first["r"][2]) == "Dentist at 3, nothing else."
    assert [d.endswith("first") for d in port.delivered] == [True]


def test_busy_assistant_is_not_typed_into_and_says_so(serve):
    p, _ = serve(FakePort(outcome=("busy", "")))
    status, _, body = _request(p, body=_chat())
    assert status == 200 and _content(body) == ve.SAY_BUSY


def test_not_running_and_needs_you_are_spoken(tmp_path):
    assert _voice(tmp_path, FakePort(outcome=("not_running", ""))).answer("hi") == ("not_running", ve.SAY_NOT_RUNNING)
    assert _voice(tmp_path, FakePort(states=[("needs_you", "")])).answer("hi") == ("needs_you", ve.SAY_NEEDS_YOU)
    assert _voice(tmp_path, FakePort(sid=None)).answer("hi") == ("sent_unwatched", ve.SAY_NO_WATCH)
    assert _voice(tmp_path, FakePort(states=[("done", "")])).answer("hi") == ("replied", ve.SAY_EMPTY)


def test_assistant_off_switch_is_checked_per_request(tmp_path, monkeypatch):
    port = FakePort()
    voice = _voice(tmp_path, port)
    monkeypatch.setenv("PANTHEON_ASSISTANT", "off")
    assert voice.answer("hi") == ("off", ve.SAY_OFF)
    assert port.delivered == []


def test_the_same_words_again_soon_are_not_typed_twice(tmp_path):
    port = FakePort()
    voice = _voice(tmp_path, port)
    first = voice.answer("what is on today?")
    assert voice.answer("what is on today?") == ("repeat", first[1])
    assert len(port.delivered) == 1
    voice.clock.t += voice.settings.repeat_window_seconds + 1
    voice.answer("what is on today?")
    assert len(port.delivered) == 2


def test_nothing_heard_is_not_typed(serve):
    port = FakePort()
    p, _ = serve(port)
    assert _content(_request(p, body=_chat("   "))[2]) == ve.SAY_NOT_HEARD
    assert port.delivered == []


# --------------------------------------------------------------------------- malformed requests


def test_body_size_cap(serve):
    port = FakePort()
    p, _ = serve(port, max_body_bytes=1024)
    status, _, _ = _request(p, body=_chat("x" * 3000))
    assert status == 413 and port.delivered == []


def test_bad_json_and_unknown_paths(serve):
    port = FakePort()
    p, _ = serve(port)
    assert _request(p, raw=b"{not json")[0] == 400
    assert _request(p, body={"messages": "nope"})[0] == 400
    assert _request(p, path="/v1/embeddings", body=_chat())[0] == 404
    assert port.delivered == []


# --------------------------------------------------------------------------- words


def test_speakable_drops_markdown_and_cuts_at_a_sentence():
    text = "## Today\n- **Dentist** at 3. See [the note](https://x.y/z).\n```\ncode\n```\nThen `gym`."
    assert ve.speakable(text, 500) == "Today Dentist at 3. See the note. Then gym."
    long = " ".join(f"Sentence number {i} is here." for i in range(50))
    out = ve.speakable(long, 120)
    assert out.endswith(ve.SAY_REST) and len(out) < 120 + len(ve.SAY_REST) + 2
    assert out.split(ve.SAY_REST)[0].rstrip().endswith(".")


# --------------------------------------------------------------------------- which puck, and where it answers

ROUTES = {"PuckA": "pc", "PuckB": "media_player.hall_speaker"}


def _with_origin(text="what is on today?", line="pantheon-voice room=Living Room puck=PuckA"):
    body = _chat(text)
    body["messages"][0] = {"role": "system", "content": f"{line}\nCurrent time is 19:13:21."}
    return body


def test_the_origin_line_names_the_room_and_puck():
    assert ve.voice_origin(_with_origin()["messages"]) == ("Living Room", "PuckA")
    assert ve.voice_origin(_with_origin(line="pantheon-voice room=None puck=PuckB")["messages"]) == ("", "PuckB")
    assert ve.voice_origin(_chat()["messages"]) == ("", "")
    assert ve.voice_origin("not a list") == ("", "")


def test_the_assistant_is_told_which_puck_heard_it(tmp_path):
    port = FakePort()
    _voice(tmp_path, port).answer("hi", ("Bedroom", "PuckB"))
    assert port.delivered == ["(said out loud to the PuckB puck in the Bedroom; answer in a few short spoken sentences) hi"]


def test_speak_on_rejects_anything_but_pc_puck_or_a_media_player(tmp_path):
    with pytest.raises(ValueError):
        ve.Settings.from_config(_cfg(tmp_path, speak_on={"PuckB": "media_player.x; rm -rf"}))
    with pytest.raises(ValueError):
        ve.Settings.from_config(_cfg(tmp_path, speak_on="pc"))
    assert ve.Settings.from_config(_cfg(tmp_path, speak_on=ROUTES)).speak_on == (
        ("pucka", "pc"), ("puckb", "media_player.hall_speaker"))


def test_a_routed_answer_plays_elsewhere_and_the_puck_stays_quiet(tmp_path):
    played = []
    voice = _voice(tmp_path, FakePort(), speak_on=ROUTES)
    voice.speaker = lambda target, words, **how: played.append((target, words)) or True
    assert voice.route("replied", "Dentist at 3.", ("Living Room", "PuckA")) == ""
    assert voice.route("timed_out", ve.SAY_TIMEOUT, ("Bedroom", "PuckB")) == ""
    assert played == [("pc", "Dentist at 3."), ("media_player.hall_speaker", ve.SAY_TIMEOUT)]
    assert voice.route("replied", "Hi.", ("Kitchen", "PuckC")) == "Hi."          # not listed: the puck says it
    assert voice.route("replied", "Hi.", ("", "")) == "Hi."
    assert len(played) == 2       # repeats are per turn: test_a_repeat_is_per_puck_and_silent_only_when_it_really_played


def test_status_lines_play_elsewhere_but_never_cut_off_a_playing_answer(tmp_path):
    played = []
    voice = _voice(tmp_path, FakePort(), speak_on=ROUTES)
    voice.speaker = lambda target, words, **how: played.append((target, how)) or True
    for code, words in (("busy_voice", ve.SAY_STILL_WORKING), ("busy", ve.SAY_BUSY), ("needs_you", ve.SAY_NEEDS_YOU),
                        ("not_running", ve.SAY_NOT_RUNNING), ("off", ve.SAY_OFF), ("not_heard", ve.SAY_NOT_HEARD)):
        assert voice.route(code, words, ("Living Room", "PuckA")) == ""
    assert all(how["preempt"] is False for _t, how in played) and len(played) == 6
    # the code a failed paste really comes back with (AssistantPort.deliver), not a made-up one
    code, words = _voice(tmp_path, FakePort(outcome=("failed", ""))).answer("hi")
    assert code == "failed" and words == ve.SAY_SEND_FAILED
    voice.route(code, words, ("Living Room", "PuckA"))
    assert played[-1][1]["preempt"] is False
    voice.route("replied", "Dentist at 3.", ("Living Room", "PuckA"))
    assert played[-1][1]["preempt"] is True          # a real answer still replaces an older one


def test_a_question_plays_to_the_end_then_the_puck_listens(tmp_path):
    played = []
    voice = _voice(tmp_path, FakePort(), speak_on=ROUTES)
    voice.speaker = lambda target, words, **how: played.append((target, words, how["until"])) or "done"
    assert voice.route("replied", "Want me to send it?", ("Living Room", "PuckA")) == ve.SAY_LISTENING
    assert voice.route("replied", "Sent it. Anything else?  ", ("Bedroom", "PuckB")) == ve.SAY_LISTENING
    assert ve.SAY_LISTENING.endswith("?")            # Home Assistant reopens the mic only on a "?"
    assert [u for *_x, u in played] == ["done", "done"]
    voice.speaker = lambda target, words, **how: ""
    assert voice.route("replied", "Want me to send it?", ("Living Room", "PuckA")) == "Want me to send it?"
    # still sounding when the wait ran out: no "Yes?", so the reopened mic cannot hear the question
    voice.speaker = lambda target, words, **how: "playing"
    assert voice.route("replied", "Want me to send it?", ("Living Room", "PuckA")) == ""


def test_a_repeat_is_per_puck_and_silent_only_when_it_really_played(tmp_path):
    played, ok = [], [True]
    port = FakePort()
    voice = _voice(tmp_path, port, speak_on=ROUTES)
    voice.speaker = lambda target, words, **how: played.append(target) or ok[0]
    av1, av2 = ("Bedroom", "PuckB"), ("Living Room", "PuckA")
    code, words = voice.answer("what time is it", av2)
    assert voice.route(code, words, av2, heard="what time is it") == "" and played == ["pc"]
    code, words = voice.answer("what time is it", av1)     # another puck: asked, not a repeat
    assert code == "replied" and len(port.delivered) == 2
    assert voice.route(code, words, av1, heard="what time is it") == ""
    assert played == ["pc", "media_player.hall_speaker"]
    code, words = voice.answer("what time is it", av2)     # PuckA again after PuckB: still a repeat
    assert code == "repeat" and len(port.delivered) == 2
    assert voice.route(code, words, av2, heard="what time is it") == "" and len(played) == 2
    ok[0] = False
    code, words = voice.answer("what is next", av1)
    assert voice.route(code, words, av1, heard="what is next") == words     # could not play: the puck says it
    ok[0] = True
    voice.route("off", ve.SAY_OFF, av2, heard="")                           # another puck's status line plays
    code, words = voice.answer("what is next", av1)
    assert code == "repeat"
    assert voice.route(code, words, av1, heard="what is next") == ""        # never played before, so played now
    assert played[-1] == "media_player.hall_speaker"


def test_the_puck_speaks_when_the_other_speaker_cannot_start(tmp_path):
    voice = _voice(tmp_path, FakePort(), speak_on=ROUTES)
    voice.speaker = lambda target, words, **how: False
    assert voice.route("replied", "Dentist at 3.", ("Living Room", "PuckA")) == "Dentist at 3."


def test_a_routed_request_returns_empty_words_to_home_assistant(serve):
    played = []
    p, voice = serve(FakePort(), speak_on=ROUTES)
    voice.speaker = lambda target, words, **how: played.append((target, words)) or True
    status, _ctype, body = _request(p, body=_with_origin())
    assert status == 200 and _content(body) == ""
    assert played == [("pc", "Dentist at 3, nothing else.")]


def test_words_files_no_player_picked_up_are_removed(tmp_path, monkeypatch):
    import os
    spool = tmp_path / "spool"
    spool.mkdir()
    old, fresh = spool / "old.txt", spool / "fresh.txt"
    old.write_text("private words", encoding="utf-8")
    fresh.write_text("still waiting", encoding="utf-8")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    ve.say_elsewhere("pc", "new words", ve.Settings.from_config(_cfg(tmp_path)), spool,
                     popen=FakePlayer(["playing"]), sleep=lambda s: None)
    names = sorted(f.name for f in spool.iterdir())
    assert names == ["fresh.txt"]     # the old one swept; the new request's own file removed once read


class FakePlayer:
    """Stands in for `voice_say.ps1`: writes the given stage words to its -StatusFile, then exits
    (or keeps running when `exits` is False)."""

    def __init__(self, stages, exits=True):
        self.stages, self.exits, self.calls = stages, exits, []

    def __call__(self, args, **kw):
        self.calls.append(args)
        with open(args[args.index("-TextFile") + 1], encoding="utf-8") as f:
            self.words = f.read()
        status = args[args.index("-StatusFile") + 1]
        if self.stages:
            with open(status, "a", encoding="utf-8") as f:
                f.write("\n".join(self.stages) + "\n")
        exits = self.exits
        return type("Proc", (), {"poll": lambda self: 0 if exits else None})()


def _say(tmp_path, player, until="playing", preempt=True):
    clock = Clock()
    ok = ve.say_elsewhere("media_player.hall_speaker", "secret words", ve.Settings.from_config(_cfg(tmp_path)),
                          tmp_path / "spool", until=until, preempt=preempt, popen=player,
                          clock=clock, sleep=clock.sleep)
    return ok, player.calls[0] if player.calls else None


def test_say_elsewhere_passes_the_words_in_a_file_never_on_the_command_line(tmp_path):
    player = FakePlayer(["playing"], exits=False)
    _ok, args = _say(tmp_path, player)
    assert "secret words" not in " ".join(args)
    assert args[args.index("-Entity") + 1] == "media_player.hall_speaker"
    text_file = args[args.index("-TextFile") + 1]
    assert text_file.endswith(".txt") and "-NoPreempt" not in args
    assert player.words == "secret words"


def test_a_posix_home_is_never_passed_to_the_player_as_appdata(tmp_path, monkeypatch):
    for name in ("USERPROFILE", "LOCALAPPDATA", "TEMP", "TMP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_mod, "HOME", "/home/someone")
    player, seen = FakePlayer(["playing"], exits=False), {}
    def popen(args, **kw):
        seen.update(kw["env"])
        return player(args, **kw)
    ve.say_elsewhere("pc", "w", ve.Settings.from_config(_cfg(tmp_path)), tmp_path / "spool",
                     popen=popen, clock=Clock(), sleep=Clock().sleep)
    assert "LOCALAPPDATA" not in seen and "TEMP" not in seen     # the script finds its own folders
    monkeypatch.setenv("USERPROFILE", "D:/profiles/someone")
    seen.clear()
    ve.say_elsewhere("pc", "w", ve.Settings.from_config(_cfg(tmp_path)), tmp_path / "spool",
                     popen=popen, clock=Clock(), sleep=Clock().sleep)
    assert seen["LOCALAPPDATA"] == "D:/profiles/someone/AppData/Local"


def test_no_words_stay_on_disk_when_the_player_dies_before_reading_them(tmp_path):
    assert _say(tmp_path, FakePlayer([]))[0] == ""                 # exited without a word
    assert _say(tmp_path, FakePlayer([], exits=False))[0] == ""    # never started; stopped
    assert not list((tmp_path / "spool").glob("*.txt"))


def test_the_puck_is_silenced_only_once_the_player_says_the_sound_started(tmp_path):
    assert _say(tmp_path, FakePlayer(["playing"], exits=False))[0] == "playing"
    assert _say(tmp_path, FakePlayer(["fail"]))[0] == ""                 # Home Assistant or the PC failed
    assert _say(tmp_path, FakePlayer([]))[0] == ""                       # ended without a word
    assert _say(tmp_path, FakePlayer(["skip"]), preempt=False)[0] == "skip"
    assert "-NoPreempt" in _say(tmp_path, FakePlayer(["skip"]), preempt=False)[1]


def test_a_whole_request_ends_before_home_assistant_would_resend_it(tmp_path):
    got = []
    clock = Clock()
    voice = _voice(tmp_path, FakePort(), speak_on=ROUTES)
    voice.clock = clock
    voice.speaker = lambda target, words, **how: got.append(how["budget"]) or "playing"
    clock.t = 500.0                                   # the Assistant took 500 s of the request already
    assert voice.route("replied", "Want me to send it?", ("Living Room", "PuckA"), started=0.0) == ""
    assert got[0] <= ve.REQUEST_BUDGET_SECONDS - 500 and ve.REQUEST_BUDGET_SECONDS < 600
    assert ve.MAX_WAIT_SECONDS < ve.REQUEST_BUDGET_SECONDS
    # say_elsewhere never waits past the budget it is given
    c2 = Clock()
    ve.say_elsewhere("pc", "w", ve.Settings.from_config(_cfg(tmp_path)), tmp_path / "spool", until="done",
                     budget=10, popen=FakePlayer(["playing"], exits=False), clock=c2, sleep=c2.sleep)
    assert c2.t <= 10.5


def test_a_player_that_never_starts_in_time_is_stopped(tmp_path):
    player = FakePlayer([], exits=False)
    killed = []
    real_call = player.__call__

    def call(args, **kw):
        proc = real_call(args, **kw)
        proc.kill = lambda: killed.append(True)
        return proc
    clock = Clock()
    state = ve.say_elsewhere("pc", "words", ve.Settings.from_config(_cfg(tmp_path)), tmp_path / "spool",
                             popen=call, clock=clock, sleep=clock.sleep)
    assert state == "" and killed == [True]          # so it cannot start late over the puck's fallback


def test_a_question_waits_for_the_sound_to_finish(tmp_path):
    assert _say(tmp_path, FakePlayer(["playing", "done"]), until="done")[0] == "done"
    assert _say(tmp_path, FakePlayer(["playing"]), until="done")[0] == ""        # cut off before the end
    assert _say(tmp_path, FakePlayer(["playing"], exits=False), until="done")[0] == "playing"  # at the cap
    assert _say(tmp_path, FakePlayer(["playing", "fail"]), until="done")[0] == ""
    assert not list((tmp_path / "spool").glob("*.status"))                       # status files cleaned up
