"""Telegram as a second way to message the pinned Assistant.

    python -m pantheon.assistant_telegram          run the poller

One process, one thread. It long-polls the Bot API's `getUpdates` (every call has a finite
deadline), takes a text message only from the ONE chat id configured, types it into the running
Assistant exactly as the deck does (only when the Assistant sits idle at an empty prompt, so it
never overwrites something typed or answers a question on its screen), waits for that turn to end
(its `Stop` hook event), and sends the reply text back with `sendMessage`.

Why not Claude Code's own Telegram channel plugin (`--channels plugin:telegram@...`): it is a
research preview that needs Bun, a restart of the live Assistant with a new flag, and the bot
token in a plain `.env` file; this poller needs none of those and reads the token the way the
notifier already does. Why not python-telegram-bot (LGPL-3, 22.x, 2026): two endpoints over
`urllib` with explicit timeouts do not justify an async framework in the MSYS2 venv.

The bot token: read from Windows Credential Manager by target name (`credential_target`) through
`notify.channels._read_credential`, kept only on the `BotApi` object, never logged, never written
anywhere. Error text never carries a URL (the token is part of every Bot API URL).

Off switches: `[assistant.telegram] enabled = false` (the default), and the Assistant's own off
switch -- `[assistant] enabled = false` or `PANTHEON_ASSISTANT=off` -- checked before every poll.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import assistant as assistant_mod
from . import config as config_mod
from . import dialogs as dialogs_mod
from . import events as events_mod
from . import tmuxctl
from . import transcripts as transcripts_mod

log = logging.getLogger("pantheon.assistant_telegram")

API_BASE = "https://api.telegram.org"
HTTP_MARGIN_SECONDS = 10          # the socket deadline is the long-poll time plus this
MAX_TELEGRAM_TEXT = 4000          # Telegram's limit is 4096 characters per message
PASTE_BUFFER = "pantheon-telegram"
STATE_FILE = "assistant_telegram.json"   # only the next update offset, never message text

# What the user reads in Telegram. Plain, short, no jargon.
MSG_HELLO = "This chat reaches your Pantheon Assistant. Send a message and its answer comes back here."
MSG_TEXT_ONLY = "Only text messages reach the Assistant for now."
MSG_TOO_OLD = ("This message is from {when}, more than {minutes} minutes before this chat was listening, "
               "so it was not passed on. "
               "Send it again if you still want it.")
MSG_NOT_RUNNING = "Your Assistant is not running right now, so this did not reach it. It starts with the deck."
MSG_BUSY = ("Your Assistant is in the middle of something (or has a question or text on its screen), "
            "so this was not sent. Try again in a minute.")
MSG_SEND_FAILED = "Could not type this into the Assistant's window, so it was not sent."
MSG_NO_WATCH = "Sent to the Assistant. Its reply will be in the Claude app; this chat cannot follow it yet."
MSG_NEEDS_YOU = ("The Assistant stopped to ask you something on its screen. Answer it in the Claude app or "
                 "on the deck; its reply will still come here.")
MSG_EMPTY_REPLY = "The Assistant finished but wrote no reply text."
MSG_TIMEOUT = ("No answer after {minutes} minutes. The Assistant may still be working; its reply will be "
               "in the Claude app, not here.")

NEEDS_YOU_TYPES = {"permission_prompt", "elicitation_dialog", "agent_needs_input"}


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Link:
    """`[assistant.telegram]` in pantheon.toml. `chat_id` and `credential_target` fall back to
    `[notify.telegram]` (the same bot and chat as the notifier) when left empty."""

    enabled: bool = False
    chat_id: str = ""
    credential_target: str = ""
    poll_seconds: int = 25            # how long one getUpdates call may wait for a message
    reply_wait_seconds: int = 600     # how long to watch for the Assistant's answer
    max_age_minutes: int = 10         # older messages (a backlog after a restart) are not passed on
    prefix: str = "(sent from Telegram) "

    @classmethod
    def from_config(cls, cfg: config_mod.Config) -> "Link":
        raw = dict((cfg.assistant or {}).get("telegram") or {})
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in raw.items() if k in known}
        link = cls(**kwargs)
        notify = cfg.notify_settings()
        return cls(
            enabled=bool(link.enabled) and str(link.enabled).lower() not in ("false", "no", "0", "off"),
            chat_id=str(link.chat_id or notify.telegram_chat_id or "").strip(),
            credential_target=str(link.credential_target or notify.telegram_credential_target or "").strip(),
            poll_seconds=max(1, int(link.poll_seconds)),
            reply_wait_seconds=max(1, int(link.reply_wait_seconds)),
            max_age_minutes=max(1, int(link.max_age_minutes)),
            prefix=str(link.prefix),
        )


# --------------------------------------------------------------------------- the Bot API


class TelegramError(Exception):
    """A Bot API call failed. The text never contains the URL (it carries the token)."""


class BotApi:
    """`getUpdates` and `sendMessage` over urllib. `read_token` is called once, at the first
    call, and again after a 401 (a token the user replaced); `opener` is injectable for tests."""

    def __init__(self, read_token: Callable[[], Optional[str]], opener=urllib.request.urlopen):
        self._read_token = read_token
        self._opener = opener
        self._token: Optional[str] = None

    def _call(self, method: str, payload: dict, timeout: float) -> object:
        if not self._token:
            self._token = self._read_token()
            if not self._token:
                raise TelegramError("could not read the bot token (check credential_target)")
        req = urllib.request.Request(f"{API_BASE}/bot{self._token}/{method}",
                                     data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._token = None
            raise TelegramError(f"{method}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        if not isinstance(body, dict) or not body.get("ok"):
            desc = body.get("description", "?") if isinstance(body, dict) else "?"
            raise TelegramError(f"{method}: {desc}")
        return body.get("result")

    def get_updates(self, offset: int, poll_seconds: int) -> list:
        result = self._call("getUpdates", {"offset": offset, "timeout": int(poll_seconds),
                                           "allowed_updates": ["message"]},
                            timeout=poll_seconds + HTTP_MARGIN_SECONDS)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: str, text: str) -> None:
        for start in range(0, max(len(text), 1), MAX_TELEGRAM_TEXT):
            self._call("sendMessage", {"chat_id": chat_id, "text": text[start:start + MAX_TELEGRAM_TEXT]},
                       timeout=HTTP_MARGIN_SECONDS + 5)


# --------------------------------------------------------------------------- the Assistant side


def _utc(ts) -> Optional[datetime]:
    when = assistant_mod._when(ts) if isinstance(ts, str) else None
    return when


def final_reply(transcript: Path, since: datetime) -> tuple[int, str]:
    """The Assistant's answer to the message sent at `since`: `(assistant records seen after
    since, the text of its last run of assistant records with no tool call after them)` -- the
    "Let me check..." lines before a tool call are left out. A count of 0 means the turn has not
    reached the transcript yet (a late `Stop` from the turn before must not end the wait)."""
    texts: list[str] = []
    seen = 0
    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, ""
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        when = _utc(rec.get("timestamp"))
        if when is None or when < since:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        seen += 1
        if any(isinstance(p, dict) and p.get("type") == "tool_use" for p in content):
            texts = []
            continue
        texts += [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
    return seen, "\n\n".join(t.strip() for t in texts if t.strip())


class AssistantPort:
    """The Assistant's existing way in: its tmux pane. `deliver` pastes the text and presses
    Enter, only after a fresh screen read shows it idle at an empty prompt (`dialogs.idle_prompt`,
    the same check `assistant.reconnect` uses). `check` reads the hook events and the transcript."""

    def __init__(self, cfg: config_mod.Config, tmux: Optional[str] = None,
                 capture: Optional[Callable[..., str]] = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.tmux = tmux if tmux is not None else cfg.tools.tmux
        self.capture = capture or tmuxctl.capture
        self.sleep = sleep

    def deliver(self, text: str) -> tuple[str, str]:
        """`("sent", "")`, or `("not_running" | "busy" | "failed", message for the user)`."""
        place = assistant_mod.locate(self.cfg, tmux=self.tmux)
        if place is None or not place.running:
            return "not_running", MSG_NOT_RUNNING
        if not dialogs_mod.idle_prompt(self.capture(place.pane_id, tmux=self.tmux)):
            return "busy", MSG_BUSY
        ok = tmuxctl.run("set-buffer", "-b", PASTE_BUFFER, "--", text, tmux=self.tmux).returncode == 0
        ok = ok and tmuxctl.run("paste-buffer", "-p", "-d", "-b", PASTE_BUFFER, "-t", place.pane_id,
                                tmux=self.tmux).returncode == 0
        if ok:
            self.sleep(0.5)    # let Claude Code take the paste before Enter
            ok = tmuxctl.run("send-keys", "-t", place.pane_id, "Enter", tmux=self.tmux).returncode == 0
        return ("sent", "") if ok else ("failed", MSG_SEND_FAILED)

    def session_id(self) -> Optional[str]:
        return assistant_mod.learn(self.cfg)

    def check(self, since: datetime) -> tuple[str, str]:
        """`("done", reply)` once the Assistant's turn after `since` has ended, `("needs_you", "")`
        while a question waits on its screen, else `("working", "")`."""
        sid = self.session_id()
        if not sid:
            return "working", ""
        try:
            events, _errors = events_mod.read_events(self.cfg.events_file)
        except OSError:
            return "working", ""
        needs_you = done = False
        for e in events:
            if e.session_id != sid or e.agent_id:
                continue
            when = _utc(e.ts)
            if when is None or when < since:
                continue
            if e.event in ("Stop", "SessionEnd"):
                done, needs_you = True, False
            elif e.event == "Notification" and (e.notification_type or "") in NEEDS_YOU_TYPES:
                needs_you = True
            elif e.event == "PostToolUse":
                needs_you = False
        if done:
            state = assistant_mod.read_state(self.cfg)
            path = state.get("transcript_path") or ""
            if not path:
                folder = state.get("folder") or self.cfg.assistant_settings().folder
                path = str(transcripts_mod.transcript_path(sid, folder, Path(self.cfg.claude_home)))
            seen, reply = final_reply(Path(path), since)
            if seen:
                return "done", reply
        return ("needs_you", "") if needs_you else ("working", "")


# --------------------------------------------------------------------------- the poller


def _read_offset(cfg: config_mod.Config) -> int:
    try:
        return int(json.loads((Path(cfg.state_dir) / STATE_FILE).read_text(encoding="utf-8")).get("offset", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def _write_offset(cfg: config_mod.Config, offset: int) -> None:
    try:
        path = Path(cfg.state_dir) / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    except OSError:
        log.warning("could not save the Telegram offset")


class Poller:
    def __init__(self, cfg: config_mod.Config, link: Link, api, port,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), check_every: float = 2.0):
        self.cfg, self.link, self.api, self.port = cfg, link, api, port
        self.clock, self.sleep, self.now, self.check_every = clock, sleep, now, check_every
        self.offset = _read_offset(cfg)
        self.started = now()     # the age rule is for the backlog from before this start only

    def _say(self, text: str) -> None:
        try:
            self.api.send_message(self.link.chat_id, text)
        except TelegramError as exc:
            log.warning("could not send to Telegram: %s", exc)

    def handle(self, update: dict) -> str:
        """One update. Returns what happened (for the log and the tests); never raises."""
        msg = update.get("message") if isinstance(update, dict) else None
        if not isinstance(msg, dict):
            return "skipped"
        chat = str((msg.get("chat") or {}).get("id", ""))
        if not self.link.chat_id or chat != self.link.chat_id:
            log.info("ignored a message from a chat that is not the configured one")
            return "denied"       # no reply: a stranger learns nothing about the bot
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            self._say(MSG_TEXT_ONLY)
            return "not_text"
        if text.strip() == "/start":
            self._say(MSG_HELLO)
            return "hello"
        sent = datetime.fromtimestamp(int(msg.get("date") or 0), timezone.utc)
        # Only a message sent long before the poller started (it was down) is stale; one that
        # queued while an earlier reply was awaited is not.
        if self.started - sent > timedelta(minutes=self.link.max_age_minutes):
            self._say(MSG_TOO_OLD.format(when=sent.astimezone().strftime("%H:%M"),
                                         minutes=self.link.max_age_minutes))
            return "too_old"
        since = self.now()
        outcome, words = self.port.deliver(self.link.prefix + text)
        if outcome != "sent":
            self._say(words)
            return outcome
        return self._wait_for_reply(since)

    def _wait_for_reply(self, since: datetime) -> str:
        if not self.port.session_id():
            self._say(MSG_NO_WATCH)
            return "sent_unwatched"
        deadline = self.clock() + self.link.reply_wait_seconds
        told_needs_you = False
        while self.clock() < deadline:
            state, reply = self.port.check(since)
            if state == "done":
                self._say(reply or MSG_EMPTY_REPLY)
                return "replied"
            if state == "needs_you" and not told_needs_you:
                self._say(MSG_NEEDS_YOU)
                told_needs_you = True
            self.sleep(self.check_every)
        self._say(MSG_TIMEOUT.format(minutes=max(1, self.link.reply_wait_seconds // 60)))
        return "timed_out"

    def run(self, max_cycles: Optional[int] = None) -> str:
        """Poll until an off switch is thrown (or `max_cycles` polls, for tests). Returns why it
        stopped."""
        backoff = 0.0
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            if not self.cfg.assistant_settings().enabled:
                log.info("the Assistant is switched off; the Telegram poller stops")
                return "off"
            try:
                updates = self.api.get_updates(self.offset, self.link.poll_seconds)
                backoff = 0.0
            except TelegramError as exc:
                backoff = min(60.0, backoff * 2 or 5.0)
                log.warning("getUpdates failed (%s); next try in %.0f s", exc, backoff)
                self.sleep(backoff)
                continue
            for update in updates:
                try:
                    self.offset = max(self.offset, int(update.get("update_id", -1)) + 1)
                except (TypeError, ValueError, AttributeError):
                    continue
                _write_offset(self.cfg, self.offset)   # saved before handling: never delivered twice
                log.info("telegram update: %s", self.handle(update))
        return "cycles"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pantheon.assistant_telegram",
                                     description="Telegram into the Pantheon Assistant")
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = config_mod.load()
    link = Link.from_config(cfg)
    if not cfg.assistant_settings().enabled:
        print("The Assistant is switched off ([assistant] enabled or PANTHEON_ASSISTANT); nothing to do.")
        return 0
    if not link.enabled:
        print("Telegram for the Assistant is off: set [assistant.telegram] enabled = true (docs/assistant-telegram.md).")
        return 0
    if not link.chat_id or not link.credential_target:
        print("Missing chat_id or credential_target in [assistant.telegram] (docs/assistant-telegram.md).")
        return 2
    from .notify.channels import _read_credential
    api = BotApi(lambda: _read_credential(link.credential_target, cfg.tools.pwsh))
    reason = Poller(cfg, link, api, AssistantPort(cfg)).run()
    print(f"Telegram poller stopped: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
