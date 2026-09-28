"""The three senders: every one returns `(ok, sentence)` and
never raises. `subprocess.run`/`Popen` and `urllib.request.urlopen` are all injected as fakes --
nothing here starts a real process or opens a real socket.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error

import pytest

from pantheon.notify import channels


# --------------------------------------------------------------------------- fakes


class FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class FakePopen:
    """Records the argv it was started with; never actually spawns anything."""

    instances: list[list[str]] = []

    def __init__(self, argv, **kw):
        self.argv = argv
        self.kw = kw
        FakePopen.instances.append(argv)


class FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def raising_opener(exc):
    def _opener(req, timeout=None):
        raise exc
    return _opener


# --------------------------------------------------------------------------- toast


def test_toast_starts_popen_and_never_waits():
    FakePopen.instances.clear()
    ok, sentence = channels.toast("Claude needs you", "loom-os", pwsh="pwsh.exe", popen=FakePopen)
    assert ok is True and "started" in sentence
    assert FakePopen.instances[-1][:3] == ["pwsh.exe", "-NoProfile", "-File"]
    assert "Claude needs you" in FakePopen.instances[-1]


def test_toast_never_raises_when_popen_fails():
    def boom(*a, **k):
        raise OSError("no pwsh")

    ok, sentence = channels.toast("t", "b", pwsh="pwsh.exe", popen=boom)
    assert ok is False and "toast failed" in sentence


# --------------------------------------------------------------------------- ntfy


def test_ntfy_refuses_cleanly_with_no_url():
    ok, sentence = channels.ntfy("", "pantheon", "t", "b")
    assert ok is False and "no server url" in sentence


def test_ntfy_success():
    ok, sentence = channels.ntfy("http://100.1.2.3:2586", "pantheon", "t", "b",
                                 opener=lambda req, timeout=None: FakeResponse(200))
    assert ok is True and sentence == "ntfy sent"


def test_ntfy_http_error_never_raises():
    ok, sentence = channels.ntfy(
        "http://100.1.2.3:2586", "pantheon", "t", "b",
        opener=raising_opener(urllib.error.HTTPError("u", 500, "boom", {}, None)),
    )
    assert ok is False and "500" in sentence


def test_ntfy_connection_error_never_raises():
    ok, sentence = channels.ntfy(
        "http://100.1.2.3:2586", "pantheon", "t", "b",
        opener=raising_opener(urllib.error.URLError("no route")),
    )
    assert ok is False and "ntfy failed" in sentence


# --------------------------------------------------------------------------- telegram


def test_telegram_refuses_with_no_chat_id():
    ok, sentence = channels.telegram("hi", chat_id="", credential_target="pantheon-telegram", pwsh="pwsh.exe")
    assert ok is False and "chat_id" in sentence


def test_telegram_refuses_with_no_credential_target():
    ok, sentence = channels.telegram("hi", chat_id="123", credential_target="", pwsh="pwsh.exe")
    assert ok is False and "credential_target" in sentence


def test_telegram_refuses_when_the_credential_cannot_be_read():
    ok, sentence = channels.telegram(
        "hi", chat_id="123", credential_target="pantheon-telegram", pwsh="pwsh.exe",
        run=lambda *a, **k: FakeCompleted(returncode=1, stdout=""),
    )
    assert ok is False and "could not read credential" in sentence


def test_telegram_sends_and_never_leaks_the_token_into_the_return_value():
    def fake_run(argv, **kw):
        return FakeCompleted(returncode=0, stdout="super-secret-token")

    def fake_opener(req, timeout=None):
        assert "super-secret-token" in req.full_url  # the token IS used to build the URL...
        return FakeResponse(200, json.dumps({"ok": True, "result": {"message_id": 1}}).encode())

    ok, sentence = channels.telegram(
        "hi", chat_id="123", credential_target="pantheon-telegram", pwsh="pwsh.exe",
        run=fake_run, opener=fake_opener,
    )
    assert ok is True and sentence == "telegram sent"
    assert "super-secret-token" not in sentence  # ...but never comes back out in the log line


def test_telegram_api_error_reports_the_description():
    def fake_run(argv, **kw):
        return FakeCompleted(returncode=0, stdout="tok")

    def fake_opener(req, timeout=None):
        return FakeResponse(200, json.dumps({"ok": False, "description": "chat not found"}).encode())

    ok, sentence = channels.telegram(
        "hi", chat_id="123", credential_target="pantheon-telegram", pwsh="pwsh.exe",
        run=fake_run, opener=fake_opener,
    )
    assert ok is False and "chat not found" in sentence


def test_telegram_never_raises_on_a_network_error():
    def fake_run(argv, **kw):
        return FakeCompleted(returncode=0, stdout="tok")

    ok, sentence = channels.telegram(
        "hi", chat_id="123", credential_target="pantheon-telegram", pwsh="pwsh.exe",
        run=fake_run, opener=raising_opener(urllib.error.URLError("down")),
    )
    assert ok is False and "telegram failed" in sentence


# --------------------------------------------------------------------------- credential quoting


def test_ps_quote_doubles_embedded_single_quotes():
    assert channels._ps_quote("it's-a-target") == "'it''s-a-target'"


def test_read_credential_never_raises_when_run_itself_errors():
    def boom(*a, **k):
        raise subprocess.SubprocessError("timed out")

    assert channels._read_credential("x", "pwsh.exe", run=boom) is None


def test_read_credential_empty_target_short_circuits_without_calling_run():
    called = []
    assert channels._read_credential("", "pwsh.exe", run=lambda *a, **k: called.append(1)) is None
    assert called == []
