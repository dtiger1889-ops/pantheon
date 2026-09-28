"""The account's own usage numbers, read from Anthropic's usage endpoint -- a SECOND, less-trusted
source behind the status-line captures.

Why this exists: the Claude budget card is fed by the status line a terminal Claude Code session
writes. The Desktop app writes none and headless `claude -p` reports no limits, so a desk with no
live terminal session read `no reading yet`. The fix: read
the account's usage locally as a second source, with the login Claude Code already keeps on this
PC. Nothing else here talks to the network.

How it reads, and what it never does:
  - `GET https://api.anthropic.com/api/oauth/usage` with the headers Claude Code itself sends
. Without the claude-code User-Agent the endpoint answers from a much
    tighter rate-limit bucket (anthropics/claude-code issues #30930, #31021, #31637).
  - The token is read from Claude Code's credentials file at call time only, held in a local
    variable for the one request, and never logged, printed, written to `state/`, or put in an
    exception message. Anything that could echo it goes through `scrub` first.
  - The credentials file is never refreshed or rewritten. That file is a cache only a terminal
    Claude Code session refreshes, so its token is often hours or days past `expiresAt`
: an expired token means no
    call at all, and the card keeps the status-line numbers.
  - Polite polling: at most one request every POLL_INTERVAL_SECONDS, and only from the collector
    process (`collector.collect_pass`); a 429, a 401 or any failure backs off exponentially,
    capped, with the server's Retry-After honoured when it asks for longer. The schedule lives in
    `state/usage-account.json` (no token in it) so a restarted collector keeps it; a lock file
    stops two collectors from both calling.
  - `[usage] account_read = false` in pantheon.toml turns all of it off.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..models import utcnow_iso

log = logging.getLogger("pantheon.hud.account")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
FALLBACK_VERSION = "2.1.283"   # the installed Claude Code on 2026-09-27, if nothing better is found

# Once every five minutes. The most careful public design (Claude-Code-Usage-Monitor issue #202)
# settled on three minutes with a 180 s cache, and aicooldown's source says to keep calls "at least
# ~3 minutes apart"; the endpoint's 429s are aggressive (three open claude-code issues), and the one
# suspension report (CodexBar #2366, unproven) polled every five. The status line stays the live
# source whenever a terminal session is running, so this only has to cover the quiet gaps, where
# a number a few minutes old is enough. So: above the community floor, and no faster than the one
# case that went wrong.
POLL_INTERVAL_SECONDS = 300
BACKOFF_FIRST_SECONDS = 600       # the first failure waits ten minutes ...
BACKOFF_MAX_SECONDS = 3600        # ... doubling to at most an hour
RETRY_AFTER_MAX_SECONDS = 6 * 3600
HTTP_TIMEOUT_SECONDS = 10
MAX_BODY_BYTES = 256 * 1024

# A reading is used only while it is younger than this and its window has not reset since.
ACCOUNT_MAX_AGE_SECONDS = 6 * 3600

STATE_FILE = "usage-account.json"
LOCK_FILE = "usage-account.lock"
LOCK_STALE_SECONDS = 60

# The flat per-model weekly keys the endpoint has been seen to return. Anything else `seven_day_*` is read the same way.
MODEL_WINDOW_NAMES = {
    "seven_day_opus": "Opus",
    "seven_day_sonnet": "Sonnet",
    "seven_day_oauth_apps": "OAuth apps",
    "seven_day_routines": "Routines",
    "seven_day_cowork": "Cowork",
}

HttpGet = Callable[[str, dict, float], tuple[int, dict, bytes]]


# --------------------------------------------------------------------------- redaction

def scrub(text: Any, token: Optional[str]) -> str:
    """`text` with the token (and anything shaped like a bearer header) taken out. Used on every
    string that might reach a log line, whatever produced it."""
    out = str(text)
    if token:
        out = out.replace(token, "[redacted]")
    return re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", out)


# --------------------------------------------------------------------------- the login on disk

def default_credentials_path(cfg) -> Path:
    """Where Claude Code keeps its login on Windows: `<claude_home>/.credentials.json`."""
    return Path(cfg.claude_home) / ".credentials.json"


def read_token(path, now: Optional[float] = None) -> tuple[Optional[str], str]:
    """`(token, "ok")`, or `(None, reason)` in plain words. Reads `claudeAiOauth.accessToken` and
    its `expiresAt` (epoch milliseconds). An expired token is not returned: calling with it is a
    guaranteed 401, and refreshing it is Claude Code's job, never ours. Never raises; a reason
    never contains any part of the file."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, "no Claude login on this PC"
    except (OSError, ValueError):
        return None, "could not read the Claude login"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token.strip():
        return None, "no Claude login on this PC"
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        if expires / 1000.0 <= now:
            return None, "the saved login is out of date until a Claude Code terminal refreshes it"
    return token.strip(), "ok"


def claude_code_version(cfg) -> str:
    """The installed Claude Code version for the User-Agent: the newest status-line capture's
    `version` (what is actually running), else the newest folder under
    `~/.local/share/claude/versions`, else FALLBACK_VERSION."""
    from . import sources
    try:
        for _mtime, data in sources.read_statusline_captures(cfg.statusline_dir, limit=5):
            version = data.get("version")
            if isinstance(version, str) and re.fullmatch(r"\d+(\.\d+){1,3}", version.strip()):
                return version.strip()
    except Exception:
        pass
    try:
        folder = Path(cfg.claude_home).parent / ".local" / "share" / "claude" / "versions"
        found = [p.name for p in folder.iterdir() if re.fullmatch(r"\d+\.\d+\.\d+", p.name)]
        if found:
            return max(found, key=lambda v: tuple(int(x) for x in v.split(".")))
    except OSError:
        pass
    return FALLBACK_VERSION


def request_headers(token: str, version: str) -> dict:
    """The headers Claude Code itself sends to this endpoint."""
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": f"claude-code/{version}",
        "Accept": "application/json",
    }


# --------------------------------------------------------------------------- HTTP

def http_get(url: str, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    """One GET. Returns `(status, response headers, body)`; a network failure is status 0.
    Never raises, and nothing from the request goes into a log line here."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        try:
            exc.close()
        except Exception:
            pass
        return exc.code, hdrs, b""
    except Exception:
        return 0, {}, b""


# --------------------------------------------------------------------------- parsing

def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_z(value) -> Optional[str]:
    """Any ISO-8601 time -> `YYYY-MM-DDTHH:MM:SSZ`, the shape the status-line readings use, so a
    reset time from either source compares equal. Unparseable -> None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(value) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    pct = _num(value.get("utilization"))
    if pct is None:
        return None
    return {"pct": max(0.0, min(100.0, pct)), "resets_at": _iso_z(value.get("resets_at"))}


def parse(payload: Any) -> dict:
    """The endpoint's JSON -> `{"five_hour": {pct, resets_at} | None, "seven_day": ...,
    "models": {label: {pct, resets_at}}}`.

    Two shapes are known: flat keys (`five_hour`, `seven_day`,
    `seven_day_opus`, ... each `{utilization, resets_at}`), and on migrated accounts a `limits`
    list (`kind` = session / weekly_all / weekly_scoped, `percent`, `resets_at`, `scope.model`)
    that is the only place per-model weekly limits such as Fable appear. The list wins when it
    has entries; flat keys fill what it leaves out. Unknown fields are ignored."""
    out: dict = {"five_hour": None, "seven_day": None, "models": {}}
    if not isinstance(payload, dict):
        return out
    for key, value in payload.items():
        if key == "five_hour" or key == "seven_day":
            out[key] = _window(value)
        elif isinstance(key, str) and key.startswith("seven_day_"):
            window = _window(value)
            if window is not None:
                label = MODEL_WINDOW_NAMES.get(key) or key[len("seven_day_"):].replace("_", " ").title()
                out["models"][label] = window
    limits = payload.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            pct = _num(entry.get("percent"))
            if pct is None:
                continue
            kind = entry.get("kind")
            flat = "five_hour" if kind == "session" else "seven_day"
            window = {"pct": max(0.0, min(100.0, pct)),
                      "resets_at": _iso_z(entry.get("resets_at"))
                      or ((out.get(flat) or {}).get("resets_at"))}
            if kind == "session":
                out["five_hour"] = window
            elif kind == "weekly_all":
                out["seven_day"] = window
            elif kind == "weekly_scoped":
                scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
                model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
                label = model.get("display_name") or model.get("id") or scope.get("surface")
                if isinstance(label, str) and label:
                    out["models"][label] = window
    return out


# --------------------------------------------------------------------------- schedule + state

def state_path(cfg) -> Path:
    return Path(cfg.hud_file).with_name(STATE_FILE)


def load_state(cfg) -> dict:
    """The schedule and the last good reading, or `{}`. Never raises."""
    try:
        with open(state_path(cfg), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(cfg, state: dict) -> None:
    from .sources import write_atomic
    try:
        write_atomic(state_path(cfg), json.dumps(state, indent=2, sort_keys=True))
    except OSError as exc:
        log.warning("could not write %s: %s", state_path(cfg).name, type(exc).__name__)


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _epoch_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after(headers: dict) -> Optional[float]:
    value = (headers or {}).get("retry-after")
    seconds = _num(value)
    if seconds is None or seconds <= 0:
        return None
    return min(seconds, RETRY_AFTER_MAX_SECONDS)


def _status_words(status: int) -> str:
    if status == 429:
        return "asked to slow down (429)"
    if status in (401, 403):
        return f"login refused ({status})"
    if status == 0:
        return "could not reach Anthropic"
    return f"answered {status}"


def enabled(cfg) -> bool:
    try:
        return bool(cfg.usage_settings().account_read)
    except Exception:
        return False


def poll(cfg, now: Optional[float] = None, http: Optional[HttpGet] = None,
         credentials: Optional[Path] = None, force: bool = False) -> dict:
    """At most one request, and only when the schedule says so (`force` skips the wait but not
    the lock -- the `--once` check). Returns the saved state: `{"reading": ..., "status": ...,
    "next_at": ..., ...}`. Never raises and never lets the token out."""
    clock = now if now is not None else _now()
    state = load_state(cfg)
    if not force and clock < (_num(state.get("next_at")) or 0.0):
        return state

    lock = Path(cfg.hud_file).with_name(LOCK_FILE)
    try:
        if lock.exists() and _now() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
            lock.unlink()
    except OSError:
        pass
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except OSError:
        return state                       # another collector is mid-call
    token: Optional[str] = None
    try:
        path = credentials if credentials is not None else default_credentials_path(cfg)
        token, reason = read_token(path, now=clock)
        state["tried_at"] = _epoch_iso(clock)
        if token is None:
            # No call made: nothing to back off from. Look at the file again next interval.
            state["status"] = reason
            state["next_at"] = clock + POLL_INTERVAL_SECONDS
            _save_state(cfg, state)
            return state
        getter = http or http_get
        status, headers, body = getter(USAGE_URL, request_headers(token, claude_code_version(cfg)),
                                       HTTP_TIMEOUT_SECONDS)
        token = None
        reading = None
        if status == 200:
            try:
                reading = parse(json.loads(body.decode("utf-8", errors="replace")))
            except ValueError:
                status = -1
        if reading is not None and (reading["five_hour"] or reading["seven_day"] or reading["models"]):
            reading["fetched_at"] = _epoch_iso(clock)
            state.update(reading=reading, status="ok", backoff_s=0,
                         next_at=clock + POLL_INTERVAL_SECONDS)
        else:
            previous = _num(state.get("backoff_s")) or 0.0
            backoff = min(BACKOFF_MAX_SECONDS, max(BACKOFF_FIRST_SECONDS, previous * 2))
            wait = max(backoff, _retry_after(headers) or 0.0)
            words = "answered with nothing readable" if status in (200, -1) else _status_words(status)
            state.update(status=words, backoff_s=backoff, next_at=clock + wait)
            log.info("account usage read: %s; next try in %dm", words, int(wait // 60))
        _save_state(cfg, state)
        return state
    except Exception as exc:              # belt and braces: the message is scrubbed, never raw
        log.warning("account usage read failed: %s", scrub(type(exc).__name__, token))
        previous = _num(state.get("backoff_s")) or 0.0
        backoff = min(BACKOFF_MAX_SECONDS, max(BACKOFF_FIRST_SECONDS, previous * 2))
        state.update(status="failed", backoff_s=backoff, next_at=clock + backoff)
        _save_state(cfg, state)
        return state
    finally:
        token = None
        try:
            lock.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- folding into the picture

def _expired(resets_at: Optional[str], now: float) -> bool:
    if not resets_at:
        return False
    try:
        return datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp() <= now
    except ValueError:
        return False


def fresh_reading(state: dict, now: float) -> Optional[dict]:
    """The saved reading while it is younger than ACCOUNT_MAX_AGE_SECONDS, else None."""
    reading = state.get("reading") if isinstance(state, dict) else None
    if not isinstance(reading, dict):
        return None
    fetched = reading.get("fetched_at")
    try:
        at = datetime.fromisoformat(str(fetched).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    if now - at > ACCOUNT_MAX_AGE_SECONDS:
        return None
    return reading


def apply(usage, state: dict, now: float) -> None:
    """Fold a fresh account reading into a Claude `ProviderUsage` built from the status line.

    Per window (`five_hour`, `seven_day`): the status line keeps it when it has a number at least
    as new as the account's; otherwise the account's number, reset time and reading time go in.
    A window whose reset has passed is never used. Each window gets `<window>_source`
    (`"statusline"` / `"account"` / None). When the headline number (the 5-hour one, else the
    weekly) came from the account, `source` becomes `"account"`, which is what the screens key
    their `from your account` words on. Per-model weekly windows go in `seven_day_models`, and
    how the last read went in `account_status`."""
    for window in ("five_hour", "seven_day"):
        has = getattr(usage, f"{window}_pct", None) is not None
        setattr(usage, f"{window}_source", "statusline" if has else None)
    usage.seven_day_models = {}
    usage.account_status = {
        "status": state.get("status"),
        "next_at": _epoch_iso(state["next_at"]) if _num(state.get("next_at")) else None,
    }
    reading = fresh_reading(state, now)
    if reading is None:
        return
    fetched = reading.get("fetched_at")
    fetched_epoch = datetime.fromisoformat(fetched.replace("Z", "+00:00")).timestamp()
    for window in ("five_hour", "seven_day"):
        acc = reading.get(window)
        if not isinstance(acc, dict) or acc.get("pct") is None or _expired(acc.get("resets_at"), now):
            continue
        mine = getattr(usage, f"{window}_pct", None)
        mine_at = getattr(usage, f"{window}_as_of", None)
        try:
            mine_epoch = datetime.fromisoformat(str(mine_at).replace("Z", "+00:00")).timestamp()
        except ValueError:
            mine_epoch = None
        if mine is not None and mine_epoch is not None and mine_epoch >= fetched_epoch:
            continue                       # the status line is newer (or as new): it wins
        setattr(usage, f"{window}_pct", acc["pct"])
        setattr(usage, f"{window}_resets_at", acc.get("resets_at"))
        setattr(usage, f"{window}_as_of", fetched)
        setattr(usage, f"{window}_source", "account")
    usage.seven_day_models = {
        label: dict(w) for label, w in (reading.get("models") or {}).items()
        if isinstance(w, dict) and w.get("pct") is not None and not _expired(w.get("resets_at"), now)
    }
    headline = "five_hour" if usage.five_hour_pct is not None else "seven_day"
    if getattr(usage, f"{headline}_source", None) == "account":
        usage.source = "account"
        usage.note = "from your account"
    usage.reported_at = getattr(usage, "five_hour_as_of", None) or getattr(usage, "seven_day_as_of", None)


# --------------------------------------------------------------------------- one-off check

def main(argv: Optional[list[str]] = None) -> int:
    """`python -m pantheon.hud.account --once`: one read now (the lock and the back-off record
    still apply), printing only the parsed percentages and reset times, or the status."""
    from .. import config as config_mod
    import sys
    args = sys.argv[1:] if argv is None else argv
    if "--once" not in args:
        print("usage: python -m pantheon.hud.account --once")
        return 2
    cfg = config_mod.load()
    config_mod.ensure_state_dirs(cfg)
    state = poll(cfg, force=True)
    print(f"status: {state.get('status')}")
    reading = state.get("reading") if state.get("status") == "ok" else None
    if reading:
        for window in ("five_hour", "seven_day"):
            w = reading.get(window) or {}
            print(f"{window}: {w.get('pct')}% resets {w.get('resets_at')}")
        for label, w in (reading.get("models") or {}).items():
            print(f"weekly {label}: {w.get('pct')}% resets {w.get('resets_at')}")
    return 0 if state.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
