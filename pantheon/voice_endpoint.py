"""Voice into the pinned Assistant.

    python -m pantheon.voice_endpoint            run the endpoint (setup: docs/voice-endpoint.md)
    python -m pantheon.voice_endpoint --check    check the settings and the token file, then exit

A voice puck hears the user, Home Assistant's Assist pipeline turns the speech into text and hands it
to its conversation agent. Here that agent is Home Assistant's core llama.cpp integration (or any
other OpenAI-compatible one) pointed at this endpoint: `GET /v1/models` and
`POST /v1/chat/completions`. The last user message is typed into the running Assistant through the
same port Telegram uses (`assistant_telegram.AssistantPort`: only when it sits idle at an empty
prompt), the Assistant's final reply is read from its transcript, trimmed to something worth
hearing, and returned for the puck to speak.

Guards, all on every request:
- off by default (`[voice] enabled = false`); the Assistant's own off switch is checked per request;
- the caller's IP must be inside the allowlist (`[voice] allow`; checked before a thread is even
  started, and again in the handler);
- a Bearer token, read once from the file `[voice] token_file` names (never from pantheon.toml,
  never inside this repo, never logged), compared in constant time;
- a body size cap, and one request at a time (a second one meanwhile is told to wait).

Every spoken outcome (busy, not running, timed out) is an HTTP 200 with words, because Home
Assistant's `openai` client resends a request on 408/409/429/5xx and on a timeout -- a resend would
type the same words in twice. For the same reason the wait for a reply is capped well under that
client's 10-minute timeout.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from . import assistant_telegram as tg
from . import config as config_mod

log = logging.getLogger("pantheon.voice_endpoint")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "pantheon-assistant"
PASTE_BUFFER = "pantheon-voice"
MIN_TOKEN_CHARS = 16
MAX_WAIT_SECONDS = 480        # under the openai package's 600 s default: no resend of a slow turn
PROBE_MAX_TOKENS = 16         # Home Assistant's setup test asks for 3 tokens; a real turn for ~3000
PROBE_TOOL = "test_function"  # the one tool Home Assistant's setup test sends
MAX_HEARD_CHARS = 2000
READ_TIMEOUT_SECONDS = 15     # a caller that stalls mid-request is dropped
MAX_OPEN_CONNECTIONS = 8      # one voice turn at a time is served anyway; the rest is headroom
# Home Assistant's agent Instructions carry one line saying which puck spoke (docs/voice-endpoint.md).
ORIGIN_LINE = re.compile(r"pantheon-voice room=(?P<room>[^\n]*?) puck=(?P<puck>[^\n]*?)\s*$", re.M)
ENTITY_ID = re.compile(r"media_player\.[a-z0-9_]+")
SAY_SCRIPT = PROJECT_ROOT / "bin" / "voice_say.ps1"
POWERSHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
SPOOL_MAX_AGE_SECONDS = 600   # a words file no player picked up in ten minutes is removed
SAY_START_WAIT_SECONDS = 25   # Home Assistant makes the audio, the PC fetches it: a few seconds
SAY_DONE_WAIT_SECONDS = 150   # a spoken question of up to max_reply_chars, played to the end
REQUEST_BUDGET_SECONDS = 540  # a whole request (Assistant wait + playing elsewhere) ends under the
                              # openai client's 600 s timeout, or Home Assistant resends it

# Localhost, the private LAN ranges, and Tailscale (IPv4 100.64.0.0/10, IPv6 fd7a:115c:a1e0::/48).
DEFAULT_ALLOW = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "100.64.0.0/10", "fd7a:115c:a1e0::/48")

# What the puck says. Short, plain, said out loud to someone standing in a room.
SAY_NOT_HEARD = "I didn't catch anything to pass on."
SAY_OFF = "Your Assistant is switched off right now, so I couldn't pass that on."
SAY_NOT_RUNNING = "Your Assistant isn't running right now, so I couldn't pass that on."
SAY_BUSY = ("Your Assistant is in the middle of something, or has a question on its screen, "
            "so I didn't send that. Try again in a minute.")
SAY_STILL_WORKING = "I'm still working on the last thing you asked. Try again in a moment."
SAY_SEND_FAILED = "I couldn't type that into your Assistant's window, so it wasn't sent."
SAY_NO_WATCH = "I sent that to your Assistant. Its answer will be in the Claude app."
SAY_NEEDS_YOU = ("Your Assistant has a question waiting on its screen. "
                 "Answer it in the Claude app or on the deck.")
SAY_EMPTY = "Your Assistant finished, but didn't say anything back."
SAY_TIMEOUT = ("That's taking a while. Your Assistant is still working on it, "
               "and the answer will be in the Claude app.")
SAY_REST = "The rest is in the Claude app."
SAY_PROBE = "OK"
SAY_LISTENING = "Yes?"    # the puck's cue after a question played elsewhere; the "?" reopens its mic
# Status lines about the request itself. For a puck whose answers play elsewhere they play there
# too (a puck's own speaker can be too quiet to hear), but never over an answer still playing.
STATUS_CODES = frozenset({"not_heard", "busy_voice", "off", "busy", "not_running", "failed", "needs_you"})


# --------------------------------------------------------------------------- settings


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1", "on")


@dataclass(frozen=True)
class Settings:
    """`[voice]` in pantheon.toml. Read only here."""

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8765
    token_file: str = ""
    allow: tuple = DEFAULT_ALLOW
    max_body_bytes: int = 262144
    reply_wait_seconds: int = 90
    max_reply_chars: int = 700
    repeat_window_seconds: int = 20
    prefix: str = "(said out loud to {puck}; answer in a few short spoken sentences) "
    speak_on: tuple = ()              # (puck name lowercased, "pc" | "puck" | a media_player entity)
    pc_voice: str = "AvaNeural"       # Home Assistant Cloud voice played on this PC ("pc")
    ha_voice: str = "AvaNeural"       # the same, for a media_player target
    networks: tuple = field(default=(), compare=False)

    def __post_init__(self) -> None:
        nets = tuple(ipaddress.ip_network(str(a).strip(), strict=False) for a in self.allow)
        object.__setattr__(self, "networks", nets)

    @classmethod
    def from_config(cls, cfg: config_mod.Config) -> "Settings":
        """Raises ValueError on a value that can't be used (a bad allowlist entry, a port out of
        range): the endpoint refuses to start rather than guess."""
        raw = dict(getattr(cfg, "voice", None) or {})
        d = cls()
        port = int(raw.get("port", d.port))
        if not 0 <= port <= 65535:
            raise ValueError(f"[voice] port must be 0-65535, got {port}")
        allow = raw.get("allow", d.allow)
        if isinstance(allow, str) or not allow:
            raise ValueError("[voice] allow must be a non-empty list of addresses or ranges")
        speak_on = raw.get("speak_on", {}) or {}
        if not isinstance(speak_on, dict):
            raise ValueError("[voice.speak_on] must be a table of puck name = where its answer plays")
        routes = []
        for puck, target in speak_on.items():
            target = str(target).strip()
            if target not in ("pc", "puck") and not ENTITY_ID.fullmatch(target):
                raise ValueError(f'[voice.speak_on] {puck} = "{target}": use "pc", "puck" or a media_player entity')
            routes.append((str(puck).strip().lower(), target))
        try:
            return cls(
                enabled=_truthy(raw.get("enabled", False)),
                bind=str(raw.get("bind", d.bind)).strip() or d.bind,
                port=port,
                token_file=str(raw.get("token_file", "")).strip(),
                allow=tuple(str(a) for a in allow),
                max_body_bytes=max(1024, int(raw.get("max_body_bytes", d.max_body_bytes))),
                reply_wait_seconds=min(MAX_WAIT_SECONDS, max(5, int(raw.get("reply_wait_seconds",
                                                                          d.reply_wait_seconds)))),
                max_reply_chars=max(80, int(raw.get("max_reply_chars", d.max_reply_chars))),
                repeat_window_seconds=max(0, int(raw.get("repeat_window_seconds",
                                                         d.repeat_window_seconds))),
                prefix=str(raw.get("prefix", d.prefix)),
                speak_on=tuple(routes),
                pc_voice=str(raw.get("pc_voice", d.pc_voice)).strip() or d.pc_voice,
                ha_voice=str(raw.get("ha_voice", d.ha_voice)).strip() or d.ha_voice,
            )
        except ValueError as exc:
            raise ValueError(f"[voice] has a value that can't be used: {exc}") from None


class TokenError(Exception):
    """The token file can't be used. The message never contains the token."""


def read_token(settings: Settings, project_root: Path = PROJECT_ROOT) -> str:
    if not settings.token_file:
        raise TokenError("[voice] token_file is empty; name a file that holds the token")
    path = Path(settings.token_file).expanduser()
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        inside = True
    except ValueError:
        inside = False
    except OSError:
        raise TokenError(f"can't read the token file {path}") from None
    if inside:
        raise TokenError(f"the token file {path} is inside the Pantheon folder; keep it outside the repo")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise TokenError(f"can't read the token file {path}") from None
    if len(token) < MIN_TOKEN_CHARS or any(c.isspace() for c in token):
        raise TokenError(f"the token in {path} must be one line of at least {MIN_TOKEN_CHARS} characters")
    return token


def ip_allowed(address: str, networks) -> bool:
    """True only when `address` sits inside one of `networks` (an allowlist, never a denylist)."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in networks)


# --------------------------------------------------------------------------- words


def speakable(text: str, limit: int) -> str:
    """The Assistant's reply as something a speaker can say: no code blocks, no markdown marks, no
    raw links, and cut at a sentence end near `limit` with a pointer to the rest."""
    s = re.sub(r"```.*?```", " ", text, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"https?://\S+", "a link", s)
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", s)
    s = re.sub(r"(?m)^\s*[-*+]\s+", "", s)
    s = re.sub(r"(?m)^\s*\|?[-:| ]{3,}\|?\s*$", " ", s)     # table rule lines
    s = s.replace("|", " ")
    s = re.sub(r"\*\*|__|~~|\*", "", s)
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    cut = cut[:end + 1] if end > limit // 3 else cut[:cut.rfind(" ")].rstrip(",;:") + "..."
    return f"{cut} {SAY_REST}"


def voice_origin(messages) -> tuple[str, str]:
    """`(room, puck)` from the `pantheon-voice room=... puck=...` line Home Assistant renders into
    its system message for each request; empty strings when the line is missing or unfilled."""
    if not isinstance(messages, list):
        return "", ""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system" and isinstance(msg.get("content"), str):
            m = ORIGIN_LINE.search(msg["content"])
            if m:
                clean = [" ".join(v.split())[:40] for v in (m["room"], m["puck"])]
                return tuple("" if v in ("None", "none", "unknown") else v for v in clean)
    return "", ""


def _clean(heard) -> str:
    return " ".join(str(heard).split())[:MAX_HEARD_CHARS]


def asks_back(words: str) -> bool:
    """True when the reply ends in a question. Home Assistant reopens the puck's microphone only
    when the words it gets back end with "?" (`ChatLog.continue_conversation`), so a question
    never comes back as blank words (`Voice.route` answers it with SAY_LISTENING)."""
    return words.rstrip().endswith(("?", "？"))


def where_words(origin: tuple[str, str]) -> str:
    room, puck = origin
    if puck and room:
        return f"the {puck} puck in the {room}"
    if puck:
        return f"the {puck} puck"
    return f"a voice puck in the {room}" if room else "a voice puck"


def _windows_path(path: Path) -> str:
    """`/c/Users/...` (MSYS2 Python) as `C:/Users/...` for a Windows program's argument; any other
    MSYS2 absolute path (`/tmp/...`) through `cygpath -m`, since Windows cannot open it as is."""
    s = path.as_posix()
    m = re.match(r"^/([a-zA-Z])/(.*)$", s)
    if m:
        return f"{m[1].upper()}:/{m[2]}"
    if s.startswith("/"):
        try:
            out = subprocess.run(["cygpath", "-m", s], stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return s


def clear_stale(spool: Path, older_than: float = SPOOL_MAX_AGE_SECONDS) -> None:
    """Remove spool files a player never picked up (it died before reading its words): the
    words must not stay on disk after the turn."""
    cutoff = time.time() - older_than
    # Also the recordings a crashed PC player left in TEMP (voice_say.ps1 names them
    # pantheon_voice_<pid>.mp3; a stopped one is removed by the player that stopped it).
    temp = Path(os.environ.get("TEMP") or tempfile.gettempdir())
    for f in [*spool.glob("*"), *temp.glob("pantheon_voice_*.mp3")]:
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def say_elsewhere(target: str, words: str, settings: Settings, spool: Path, until: str = "playing",
                  preempt: bool = True, budget: float = REQUEST_BUDGET_SECONDS,
                  popen=subprocess.Popen, clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> str:
    """Run `bin/voice_say.ps1` to speak `words` on this PC ("pc") or on a Home Assistant media
    player, and wait for its word that the sound started (`until="playing"`) or finished
    (`until="done"`, for a question: the puck must not listen while the question still plays).
    The words travel in a file the script deletes, never on a command line. `preempt=False` (a
    status line) skips it while another answer plays. Returns "done" (finished), "playing"
    (started; for a question, still sounding when the wait ran out), "skip", or "" when the script
    could not start, said "fail", ended without a word, or never started in time (then it is
    stopped): on "" the puck says the words itself."""
    try:
        spool.mkdir(parents=True, exist_ok=True)
        clear_stale(spool)
        stem = uuid.uuid4().hex
        text_file, status = spool / f"{stem}.txt", spool / f"{stem}.status"
        text_file.write_text(words, encoding="utf-8")
    except OSError:
        return ""
    args = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", _windows_path(SAY_SCRIPT), "-TextFile", _windows_path(text_file),
            "-StatusFile", _windows_path(status)]
    if target == "pc":
        args += ["-Target", "pc", "-Voice", settings.pc_voice]
    else:
        args += ["-Target", "ha", "-Entity", target, "-Voice", settings.ha_voice]
    if not preempt:
        args.append("-NoPreempt")
    # The script keeps its files under TEMP and finds the Home Assistant token under LOCALAPPDATA;
    # a shell started without them (seen from a nested MSYS2 bash) would end it at once.
    env = dict(os.environ)
    # Only a Windows profile path (drive letter) is filled in; otherwise the script finds the
    # folders itself, which beats pointing it at a POSIX home that has no AppData.
    profile = env.get("USERPROFILE") or str(config_mod.HOME)
    if re.match(r"^[A-Za-z]:[\\/]", profile):
        env.setdefault("LOCALAPPDATA", f"{profile}/AppData/Local")
        env.setdefault("TEMP", f"{env['LOCALAPPDATA']}/Temp")
        env.setdefault("TMP", env["TEMP"])
    try:
        proc = popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    except OSError:
        text_file.unlink(missing_ok=True)
        return ""

    def stages() -> set:
        try:
            return set(status.read_text(encoding="utf-8").split())
        except OSError:
            return set()

    wait = SAY_DONE_WAIT_SECONDS if until == "done" else SAY_START_WAIT_SECONDS
    deadline = clock() + max(1.0, min(wait, budget))
    seen: set = set()
    try:
        while clock() < deadline:
            seen = stages()
            if "fail" in seen:
                return ""
            if "skip" in seen:
                return "skip"
            if "done" in seen:
                return "done"
            if until == "playing" and "playing" in seen:
                return "playing"
            if proc.poll() is not None:
                seen = stages()
                if "fail" in seen:
                    return ""
                return "done" if "done" in seen else ("skip" if "skip" in seen else
                                                      ("playing" if until == "playing" and "playing" in seen else ""))
            sleep(0.2)
        if "playing" in seen:
            return "playing"        # still sounding at the cap: started, but not known to be finished
        # Never started in time: stop it, so it cannot start late over the puck's own fallback.
        try:
            proc.kill()
        except (OSError, AttributeError):
            pass
        return ""
    finally:
        status.unlink(missing_ok=True)
        # A player that read its words has deleted them already; one that died first has not,
        # and nothing else would remove them until a later request.
        text_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- the service


class VoicePort(tg.AssistantPort):
    paste_buffer = PASTE_BUFFER


class Voice:
    """One heard sentence in, one thing to say out. Never raises for an Assistant-side problem;
    each outcome comes back as `(code, words)`."""

    def __init__(self, cfg: config_mod.Config, settings: Settings, port,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), check_every: float = 1.0,
                 speaker: Optional[Callable[..., bool]] = None):
        self.cfg, self.settings, self.port = cfg, settings, port
        self.clock, self.sleep, self.now, self.check_every = clock, sleep, now, check_every
        spool = Path(cfg.state_dir) / "voice_say"
        self.speaker = speaker or (lambda target, words, **how: say_elsewhere(target, words, settings, spool, **how))
        self._lock = threading.Lock()
        # Recent turns, one per (words heard, puck): [when, code, words, where-it-played state].
        # Kept per turn, so another puck's request or a status line never changes this one's.
        self._recent: dict[tuple[str, str], list] = {}
        self._recent_lock = threading.Lock()

    def target(self, origin: tuple[str, str]) -> str:
        """Where this puck's answer plays: "puck" unless `[voice.speak_on]` names it."""
        return dict(self.settings.speak_on).get(origin[1].lower(), "puck") if origin[1] else "puck"

    def _turn(self, heard: str, origin: tuple[str, str]) -> tuple[str, str]:
        return _clean(heard), origin[1].lower()

    def route(self, code: str, words: str, origin: tuple[str, str] = ("", ""), heard: str = "",
              started: Optional[float] = None) -> str:
        """The words for Home Assistant to speak on the puck: `words` when the puck plays its own
        answer; "" (Home Assistant then skips speech) once they started on the PC or another
        speaker. A question plays there to the end, then the puck says "Yes?" so it listens for
        the answer. A status line never cuts off an answer still playing. A repeat (same words,
        same puck) that already played there is not played again."""
        target = self.target(origin)
        if target == "puck" or not words:
            return words
        question = asks_back(words)
        with self._recent_lock:
            turn = self._recent.get(self._turn(heard, origin))
        if code == "repeat" and turn and turn[3]:
            return SAY_LISTENING if question and turn[3] == "done" else ""
        budget = REQUEST_BUDGET_SECONDS - (self.clock() - started) if started is not None else REQUEST_BUDGET_SECONDS
        state = self.speaker(target, words, until="done" if question else "playing",
                             preempt=code not in STATUS_CODES, budget=budget)
        if turn is not None and code not in STATUS_CODES:
            turn[3] = state
        if state:
            # The puck listens again only once the question has finished playing elsewhere; one
            # still sounding when the wait ran out gets no "Yes?", or the mic would hear it.
            return SAY_LISTENING if question and state == "done" else ""
        log.info("voice: could not start the speaker for %s; the puck speaks instead", target)
        return words

    def answer(self, heard: str, origin: tuple[str, str] = ("", "")) -> tuple[str, str]:
        key = self._turn(heard, origin)
        text = key[0]
        if not text:
            return "not_heard", SAY_NOT_HEARD
        if not self._lock.acquire(blocking=False):
            return "busy_voice", SAY_STILL_WORKING
        try:
            window = self.settings.repeat_window_seconds
            with self._recent_lock:
                now = self.clock()
                for k in [k for k, t in self._recent.items() if now - t[0] >= window]:
                    del self._recent[k]
                hit = self._recent.get(key)
            if hit and window:
                return "repeat", hit[2]     # a resend or a double wake: never typed in twice
            if not self.cfg.assistant_settings().enabled:
                return "off", SAY_OFF
            since = self.now()
            prefix = self.settings.prefix.replace("{puck}", where_words(origin))
            outcome, _words = self.port.deliver(prefix + text)
            if outcome != "sent":
                return outcome, {"not_running": SAY_NOT_RUNNING, "busy": SAY_BUSY}.get(outcome, SAY_SEND_FAILED)
            code, words = self._wait_for_reply(since)
            with self._recent_lock:
                self._recent[key] = [self.clock(), code, words, ""]
            return code, words
        finally:
            self._lock.release()

    def _wait_for_reply(self, since: datetime) -> tuple[str, str]:
        if not self.port.session_id():
            return "sent_unwatched", SAY_NO_WATCH
        deadline = self.clock() + self.settings.reply_wait_seconds
        while self.clock() < deadline:
            state, reply = self.port.check(since)
            if state == "done":
                words = speakable(reply, self.settings.max_reply_chars) if reply else ""
                return "replied", words or SAY_EMPTY
            if state == "needs_you":
                return "needs_you", SAY_NEEDS_YOU
            self.sleep(self.check_every)
        return "timed_out", SAY_TIMEOUT


# --------------------------------------------------------------------------- the request


def last_user_text(messages) -> str:
    """The newest `user` message's text (a plain string, or the text parts of a list)."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
        return ""
    return ""


def is_setup_probe(body: dict) -> bool:
    """Home Assistant's integration setup test (3 tokens, one tool called `test_function`). It must
    be answered here, never typed into the Assistant."""
    for key in ("max_tokens", "max_completion_tokens"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value <= PROBE_MAX_TOKENS:
            return True
    tools = body.get("tools")
    if isinstance(tools, list):
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if isinstance(fn, dict) and fn.get("name") == PROBE_TOOL:
                return True
    return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "pantheon-voice"
    sys_version = ""
    protocol_version = "HTTP/1.0"     # one request per connection: the allowlist sees every one
    timeout = READ_TIMEOUT_SECONDS

    def log_message(self, fmt, *args) -> None:     # no request lines on stderr; outcomes are logged
        return

    # ---- replies
    def _send_json(self, code: int, obj: dict, headers: Optional[dict] = None) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str, kind: str, headers: Optional[dict] = None) -> None:
        self._send_json(code, {"error": {"message": message, "type": kind}}, headers)

    def _send_completion(self, words: str, model: str, stream: bool) -> None:
        cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
        if not stream:
            self._send_json(200, {
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": words},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
            return
        chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
        parts = [
            {**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": words},
                                   "finish_reason": None}]},
            {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        data = "".join(f"data: {json.dumps(p)}\n\n" for p in parts) + "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))

    # ---- gates
    def _gate(self) -> bool:
        if not ip_allowed(self.client_address[0], self.server.settings.networks):
            log.info("refused a caller outside the allowlist")
            self._error(403, "not allowed from this address", "forbidden")
            return False
        got = self.headers.get("Authorization", "")
        scheme, _, presented = got.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip().encode("utf-8"),
                                                                 self.server.token.encode("utf-8")):
            log.info("refused a request with a missing or wrong token from %s", self.client_address[0])
            self._error(401, "missing or wrong API key", "invalid_api_key", {"WWW-Authenticate": "Bearer"})
            return False
        return True

    def _route(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/")

    # ---- methods
    def do_GET(self) -> None:
        if not self._gate():
            return
        if self._route() in ("/v1/models", "/models"):
            self._send_json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "pantheon"}]})
            return
        self._error(404, "not found", "not_found")

    def do_POST(self) -> None:
        if not self._gate():
            return
        if self._route() not in ("/v1/chat/completions", "/chat/completions"):
            self._error(404, "not found", "not_found")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(411, "Content-Length is required", "invalid_request_error")
            return
        if length < 0 or length > self.server.settings.max_body_bytes:
            self._error(413, "request too large", "invalid_request_error")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (socket.timeout, OSError):
            return
        except (ValueError, UnicodeDecodeError):
            self._error(400, "body is not JSON", "invalid_request_error")
            return
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            self._error(400, "messages must be a list", "invalid_request_error")
            return
        model = str(body.get("model") or MODEL_ID)
        stream = body.get("stream") is True
        if is_setup_probe(body):
            log.info("voice: setup_probe")
            self._send_completion(SAY_PROBE, model, stream)
            return
        origin = voice_origin(body["messages"])
        voice = self.server.voice
        heard = last_user_text(body["messages"])
        started = voice.clock()
        code, words = voice.answer(heard, origin)
        target = voice.target(origin)
        log.info("voice: %s from %s, answer on %s", code, origin[1] or "an unnamed puck", target)
        self._send_completion(voice.route(code, words, origin, heard=heard, started=started), model, stream)


class VoiceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False     # no second process can share the port

    def __init__(self, address, voice: Voice, settings: Settings, token: str):
        self.voice, self.settings, self.token = voice, settings, token
        self._open: set = set()
        self._open_lock = threading.Lock()
        super().__init__(address, _Handler)

    def verify_request(self, request, client_address) -> bool:
        """Runs on the accept loop, before a thread or a byte of parsing: outside the allowlist,
        the connection is closed with no answer. Past `MAX_OPEN_CONNECTIONS` at once (each one a
        thread, even before its token is read) a new one is closed the same way, so slow callers
        inside the allowlist cannot pile up threads."""
        if not ip_allowed(client_address[0], self.settings.networks):
            log.info("dropped a connection from outside the allowlist")
            return False
        with self._open_lock:
            if len(self._open) >= MAX_OPEN_CONNECTIONS:
                log.info("dropped a connection: %d already open", len(self._open))
                return False
            self._open.add(request)
        return True

    def shutdown_request(self, request) -> None:
        with self._open_lock:
            self._open.discard(request)
        super().shutdown_request(request)


class VoiceServer6(VoiceServer):
    address_family = socket.AF_INET6


def make_server(voice: Voice, settings: Settings, token: str) -> VoiceServer:
    cls = VoiceServer6 if ":" in settings.bind else VoiceServer
    return cls((settings.bind, settings.port), voice, settings, token)


# --------------------------------------------------------------------------- main


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pantheon.voice_endpoint",
                                     description="Voice (Home Assistant) into the Pantheon Assistant")
    parser.add_argument("--check", action="store_true", help="check settings and token file, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = config_mod.load()
    try:
        settings = Settings.from_config(cfg)
    except ValueError as exc:
        print(str(exc))
        return 2
    if not settings.enabled:
        print("Voice into the Assistant is off: set [voice] enabled = true (docs/voice-endpoint.md).")
        return 0
    if not cfg.assistant_settings().enabled:
        print("The Assistant is switched off ([assistant] enabled or PANTHEON_ASSISTANT); nothing to do.")
        return 0
    try:
        token = read_token(settings)
    except TokenError as exc:
        print(f"Voice endpoint not started: {exc} (docs/voice-endpoint.md).")
        return 2
    if args.check:
        print(f"Voice settings OK: would listen on {settings.bind}:{settings.port}, "
              f"allowing {', '.join(settings.allow)}.")
        return 0
    if settings.bind in ("0.0.0.0", "::"):
        log.warning("listening on every network interface; the allowlist is the only address guard")
    voice = Voice(cfg, settings, VoicePort(cfg))
    server = make_server(voice, settings, token)
    print(f"Voice endpoint listening on {settings.bind}:{server.server_address[1]} (Ctrl-C stops it).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("Voice endpoint stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
