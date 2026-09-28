"""Where the HUD's numbers come from.

Three local sources:
  1. the JSON Claude Code pipes to its statusLine command, captured by `bin/statusline_capture`
  2. `ccusage` over the local Claude and Codex logs (history, and cost when nothing is live)
  3. Codex's own session logs, which sometimes carry the real rate-limit percentages
and one network read, second to the status line: the account's own usage numbers
. Newer status-line numbers always win over it.

Nothing here raises at the caller. A source that cannot answer returns `None` fields and says
so in `ProviderUsage.source` / `.note`; the screen prints `?`, never an invented number.
Only numbers are read out of the logs -- no prompt text, no auth files.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..models import ProviderUsage, utcnow_iso
from . import calc

log = logging.getLogger("pantheon.hud.sources")

SUBPROCESS_TIMEOUT_SECONDS = 60
HISTORY_SAMPLES = 30          # how many readings the sparkline and the burn rate look back over
STATUSLINE_MAX_AGE_SECONDS = 600   # older than this, a reading is shown with its age beside it
STATUSLINE_SCAN_FILES = 60          # newest captures read for the account-wide percentages
CODEX_LOG_FILES_SCANNED = 5   # newest few only; there are hundreds
CODEX_LOG_TAIL_BYTES = 512 * 1024


# --------------------------------------------------------------------------- helpers

def _iso(epoch: Optional[float]) -> Optional[str]:
    """Unix epoch SECONDS -> ISO-8601 UTC. Claude Code and Codex both use epoch seconds."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, then rename -- a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_json(argv: list[str], env: Optional[dict] = None, timeout: int = SUBPROCESS_TIMEOUT_SECONDS):
    """Run a command that prints JSON. Returns the parsed object, or None on any failure.

    Never raises: a missing Node, a slow npx, a non-zero exit and unparseable output all come
    back as None so the HUD keeps drawing the numbers it does have.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=timeout, check=False
        )
    except Exception as exc:  # FileNotFoundError, TimeoutExpired, OSError...
        log.warning("usage command failed: %s: %s", argv[0], exc)
        return None
    if proc.returncode != 0:
        log.warning("usage command exit %s: %s", proc.returncode, (proc.stderr or "")[:300])
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        start = out.find("{")
        if start > 0:  # npx sometimes prints an install notice before the JSON
            try:
                return json.loads(out[start:])
            except json.JSONDecodeError:
                pass
        log.warning("usage command did not print JSON: %s", out[:200])
        return None


def _ccusage_argv(cfg, *args: str) -> list[str]:
    """A local ccusage binary when `[tools] ccusage` is set (no npx, no npm cache in the hot path);
    otherwise npx with the pinned version."""
    local = getattr(cfg.tools, "ccusage", "") or ""
    if local and os.path.exists(local):
        return [local, *args]
    return [cfg.tools.npx, f"ccusage@{cfg.ccusage_version}", *args]


def _today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _today_row(daily: Iterable[dict] | None, today: Optional[str] = None) -> Optional[dict]:
    """The daily row for today, if ccusage has one. Nothing today -> None (not the newest row:
    yesterday's cost printed as 'today' would be a fabricated number).

    The two subcommands name the day differently -- `ccusage daily` calls it `period`,
    `ccusage codex` calls it `date` -- so both are accepted."""
    today = today or _today_local()
    for row in daily or ():
        if not isinstance(row, dict):
            continue
        if str(row.get("period") or row.get("date") or "") == today:
            return row
    return None


def _row_cost_and_tokens(row: Optional[dict], agent: Optional[str] = None):
    """Cost and tokens out of one daily row, for one agent.

    `ccusage daily` on this machine reports BOTH agents added together (`agent: "all"`), which
    would print Codex's spend inside the Claude block. With `--by-agent` the row also carries an
    `agents` list, and that is what gets picked out. If the row cannot be narrowed to the agent
    asked for, this returns nothing rather than a number that is quietly too big.
    `totalCost`/`costUSD` are the two names ccusage uses for the same figure.
    """
    if not isinstance(row, dict):
        return None, None
    if agent:
        breakdown = row.get("agents")
        if isinstance(breakdown, list):
            row = next(
                (r for r in breakdown if isinstance(r, dict) and r.get("agent") == agent), None
            )
            if row is None:
                return None, None
        elif str(row.get("agent") or agent) not in (agent, ""):
            return None, None
    cost = _num(row.get("totalCost"))
    if cost is None:
        cost = _num(row.get("costUSD"))
    tokens = _num(row.get("totalTokens"))
    return cost, (int(tokens) if tokens is not None else None)


# --------------------------------------------------------------------------- 1. statusline

def read_statusline_files(directory, max_age_s: int = STATUSLINE_MAX_AGE_SECONDS,
                          now: Optional[float] = None) -> list[dict]:
    """Every fresh capture in `directory`, newest first. Stale and unreadable files are skipped."""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    fresh: list[tuple[float, dict]] = []
    for path in folder.glob("*.json"):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if max_age_s is not None and age > max_age_s:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            fresh.append((path.stat().st_mtime, data))
    fresh.sort(key=lambda pair: pair[0], reverse=True)
    return [data for _, data in fresh]


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _window_expired(reset_epoch: Optional[float], now: float) -> bool:
    """A window whose reset time has passed: its used-percent is from before the reset and is
    no longer a number about anything. Codex's log said `5h 50%` nine hours after that window
    had reset. Unknown reset time -> not expired."""
    return reset_epoch is not None and reset_epoch <= now


def read_statusline_captures(directory, limit: int = STATUSLINE_SCAN_FILES) -> list[tuple[float, dict]]:
    """Every capture on disk with its file time (epoch seconds), newest first, capped at the
    newest `limit` files. Unlike `read_statusline_files` nothing is skipped for age: an old
    capture still carries the last known percentages, and the reader decides how to show its age.
    Unreadable files are skipped; a missing folder is an empty list. Never raises."""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    stamped: list[tuple[float, Path]] = []
    for path in folder.glob("*.json"):
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    stamped.sort(key=lambda pair: pair[0], reverse=True)
    out: list[tuple[float, dict]] = []
    for mtime, path in stamped[: max(1, limit)]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append((mtime, data))
    return out


def best_readings(captures: Iterable[tuple[float, dict]], now: float) -> dict:
    """The account-wide reading per window (`five_hour`, `seven_day`) out of many captures:
    `{window: (percent, reset_epoch, as_of_epoch)}`, a window with no usable reading left out.

    Why not simply the newest capture: Claude Code drops a
    window from a session's payload once that window's reset time passes, and brings it back only
    after that session's next reply. An idle session keeps re-rendering (it did at 20:10:01Z and
    01:10:02Z, both exact reset times) with no `five_hour` and a weekly number a day old. So:
    a window whose reset has passed is skipped; the latest reset time wins (it is the current
    window); inside one window the higher percent wins, because a window's percent only rises;
    on a tie, the newer capture, so the age shown is the youngest honest one."""
    ranked: dict[str, tuple[tuple, tuple]] = {}
    for mtime, data in captures:
        for window in ("five_hour", "seven_day"):
            pct = _num(_dig(data, "rate_limits", window, "used_percentage"))
            if pct is None:
                continue
            reset = _num(_dig(data, "rate_limits", window, "resets_at"))
            if _window_expired(reset, now):
                continue
            key = (reset if reset is not None else 0.0, pct, mtime)
            if window not in ranked or key > ranked[window][0]:
                ranked[window] = (key, (pct, reset, mtime))
    return {window: reading for window, (_key, reading) in ranked.items()}


def claude_from_statusline(directory, max_age_s: int = STATUSLINE_MAX_AGE_SECONDS,
                           now: Optional[float] = None) -> ProviderUsage:
    """Budget numbers from the captures Claude Code sessions write.

    Several sessions report the same account-wide allowance, so the percentages come from
    `best_readings` over every capture on disk, fresh or not, each with the time it was written
    (`five_hour_as_of` / `seven_day_as_of`, ISO-8601 UTC; `reported_at` is the 5-hour one, or the
    weekly one when there is no 5-hour). A reading older than `max_age_s` is still the last known
    number -- percentages only rise inside a window, so it is a floor, never an overstatement --
    and the screens print its age beside it instead of a `?`.
    The context window comes from the newest capture, and only while it is fresh: it describes
    one session, and an hour-old one is about nobody the user is looking at.
    `rate_limits` only exists on Pro/Max and only after the first reply -- every field is optional.
    """
    usage = ProviderUsage(provider="claude", source="statusline", fetched_at=utcnow_iso())
    usage.five_hour_as_of = None
    usage.seven_day_as_of = None
    clock = now if now is not None else _now_epoch()
    captures = read_statusline_captures(directory)
    if not captures:
        usage.source = "unknown"
        usage.note = "no live session"
        return usage

    best = best_readings(captures, clock)
    for window, pct_field, reset_field, as_of_field in (
        ("five_hour", "five_hour_pct", "five_hour_resets_at", "five_hour_as_of"),
        ("seven_day", "seven_day_pct", "seven_day_resets_at", "seven_day_as_of"),
    ):
        reading = best.get(window)
        if reading is None:
            continue
        pct, reset, as_of = reading
        setattr(usage, pct_field, pct)
        setattr(usage, reset_field, _iso(reset))
        setattr(usage, as_of_field, _iso(as_of))
    usage.reported_at = usage.five_hour_as_of or usage.seven_day_as_of

    newest_mtime, newest = captures[0]
    fresh = max_age_s is None or clock - newest_mtime <= max_age_s
    if fresh:
        usage.context_pct = _num(_dig(newest, "context_window", "used_percentage"))
    if usage.five_hour_pct is None and usage.seven_day_pct is None:
        if fresh:
            # A live session with no rate_limits block (free/API plan, or before the first reply).
            usage.note = "no budget numbers in this session"
        else:
            usage.source = "unknown"
            usage.note = "no live session"
    return usage


def statusline_for(directory, session_id: Optional[str]) -> dict:
    """The newest statusline capture for exactly one session: `bin/statusline_capture`
    writes `<directory>/<session_id>.json` on every re-render, so this is a single-file read, not
    a scan of the whole folder like `read_statusline_files` does for the HUD strip's account-wide
    percentages. Used to show and confirm a row's current model/effort after `session_ctl` types a
    change into it. Missing, unreadable, or no session id -> `{}`, never raised."""
    if not session_id:
        return {}
    path = Path(directory) / f"{session_id}.json"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def statusline_mtimes(directory) -> dict[str, float]:
    """`{session_id: epoch_seconds}` for every statusline capture on disk: the
    supervisor's liveness rule (`supervisor/state.py` `_presence_age`) counts a fresh capture as
    presence for a session with no tmux pane, because a live Claude Desktop session re-renders its
    statusline on every turn even though it writes no tmux-visible event of its own. A missing
    folder, or a file whose mtime cannot be read, is simply left out -- never raised."""
    folder = Path(directory)
    if not folder.is_dir():
        return {}
    out: dict[str, float] = {}
    for path in folder.glob("*.json"):
        try:
            out[path.stem] = path.stat().st_mtime
        except OSError:
            continue
    return out


def statusline_line(data: dict, account: Optional[ProviderUsage] = None,
                    now: Optional[float] = None) -> str:
    """The one short line `bin/statusline_capture` prints back so Claude Code's own status bar
    keeps working, e.g. `5h 61% · wk 44% · ctx 32% · $27.18 · Fable 5.1`.

    The 5-hour and weekly numbers are account-wide, so they come from `account` (what
    `claude_from_statusline` reads over every capture) when given: an idle session's own payload
    has no `five_hour` after a reset and a weekly number from its last reply, which is how the
    garden_plans window came to say `5h ? · wk 29%` while the account stood at 61% / 44%
. A reading older than ten minutes carries its age (`5h 61% 14m ago`); a number
    nobody has reported is left out rather than printed as `?`. Context, cost and model are this
    session's own."""
    clock = now if now is not None else _now_epoch()

    def window(name: str, label: str) -> Optional[str]:
        if account is not None:
            value = getattr(account, f"{name}_pct", None)
            as_of = getattr(account, f"{name}_as_of", None)
        else:
            value, as_of = _num(_dig(data, "rate_limits", name, "used_percentage")), None
        if value is None:
            return None
        age = calc.stale_age_words(as_of, clock)
        return f"{label} {value:.0f}%" + (f" {age}" if age else "")

    context = _num(_dig(data, "context_window", "used_percentage"))
    cost = _num(_dig(data, "cost", "total_cost_usd"))
    model = _dig(data, "model", "display_name") or _dig(data, "model", "id")
    parts = [
        window("five_hour", "5h"),
        window("seven_day", "wk"),
        f"ctx {context:.0f}%" if context is not None else None,
        f"${cost:.2f}" if cost is not None else None,
        str(model) if model else None,
    ]
    kept = [p for p in parts if p]
    return " · ".join(kept) if kept else "no usage reading yet"


# --------------------------------------------------------------------------- 2. ccusage

def claude_from_ccusage(cfg, timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
                        blocks: Optional[dict] = None, daily: Optional[dict] = None) -> ProviderUsage:
    """Claude cost, burn and block-reset time read back out of the local logs.

    ccusage knows nothing about the subscription percentage, so the percent fields stay None --
    that is what `from history` on the screen means. `blocks`/`daily` are injectable for tests.
    """
    usage = ProviderUsage(
        provider="claude", source="ccusage", note="from history", fetched_at=utcnow_iso()
    )
    env = cfg.node_env()
    if blocks is None:
        blocks = run_json(_ccusage_argv(cfg, "blocks", "--json", "--active"), env=env, timeout=timeout)
    if daily is None:
        # `--by-agent` splits Claude out from Codex; `--last 2` keeps the payload to yesterday
        # and today, which is all "today's cost" needs.
        daily = run_json(
            _ccusage_argv(cfg, "daily", "--json", "--by-agent", "--last", "2"),
            env=env, timeout=timeout,
        )

    block = None
    for candidate in (_dig(blocks, "blocks") or []):
        if isinstance(candidate, dict) and candidate.get("isActive") and not candidate.get("isGap"):
            block = candidate
            break
    if block:
        usage.burn_cost_per_hour = _num(_dig(block, "burnRate", "costPerHour"))
        usage.five_hour_resets_at = block.get("endTime") or None

    row = _today_row(_dig(daily, "daily"))
    if row:
        usage.cost_today_usd, usage.tokens_today = _row_cost_and_tokens(row, agent="claude")

    if block is None and row is None:
        usage.source = "unknown"
        usage.note = "no usage history found"
    return usage


def codex_from_ccusage(cfg, timeout: int = SUBPROCESS_TIMEOUT_SECONDS,
                       payload: Optional[dict] = None) -> ProviderUsage:
    """Codex cost and tokens for today, from `ccusage codex --json` over `~/.codex/sessions`."""
    usage = ProviderUsage(
        provider="codex", source="ccusage", note="from history", fetched_at=utcnow_iso()
    )
    if payload is None:
        payload = run_json(_ccusage_argv(cfg, "codex", "--json"), env=cfg.node_env(), timeout=timeout)
    row = _today_row(_dig(payload, "daily"))
    if row is None:
        if payload is None:
            usage.source = "unknown"
            usage.note = "no usage history found"
        else:
            usage.cost_today_usd = 0.0
            usage.tokens_today = 0
            usage.note = "nothing run today"
        return usage
    usage.cost_today_usd, usage.tokens_today = _row_cost_and_tokens(row, agent="codex")
    return usage


# --------------------------------------------------------------------------- 3. codex logs

def _tail_lines(path: Path, max_bytes: int = CODEX_LOG_TAIL_BYTES) -> list[str]:
    """The last `max_bytes` of a session log, split into lines. The first line may be a
    fragment; the caller parses each line separately and drops what will not parse."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            raw = fh.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def _find_rate_limits(obj: Any, depth: int = 0) -> Optional[dict]:
    """Codex nests `rate_limits` under `payload`; find it wherever it sits, without walking
    into the message text."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    found = obj.get("rate_limits")
    if isinstance(found, dict):
        return found
    for key in ("payload", "info", "data"):
        nested = _find_rate_limits(obj.get(key), depth + 1)
        if nested is not None:
            return nested
    return None


def codex_rate_limits(codex_home, files_scanned: int = CODEX_LOG_FILES_SCANNED,
                      now: Optional[float] = None) -> ProviderUsage:
    """Codex's own 5-hour and weekly percentages, when its logs happen to carry them.

    Codex writes a `rate_limits` object beside its token counts; `primary`/`secondary` are
    keyed by `window_minutes` (300 = 5 hours, 10080 = a week) and are often null
    (openai/codex issue #14880). Missing stays missing -- the screen shows `?`.
    The log line's own timestamp goes into `reported_at` (the screen says `as of HH:MM`), and a
    window whose reset time has passed by `now` (epoch seconds, default: the clock) is dropped
    rather than shown as if current. Only these numbers are read; no prompt text and no auth
    file is ever touched.
    """
    usage = ProviderUsage(provider="codex", source="codex-log", fetched_at=utcnow_iso())
    clock = now if now is not None else _now_epoch()
    root = Path(codex_home) / "sessions"
    if not root.is_dir():
        usage.source = "unknown"
        usage.note = "no Codex logs found"
        return usage

    try:
        logs = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        logs = []
    for path in logs[: max(1, files_scanned)]:
        for line in reversed(_tail_lines(path)):
            line = line.strip()
            if not line or '"rate_limits"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            limits = _find_rate_limits(record)
            if not limits:
                continue
            found = False
            for key in ("primary", "secondary"):
                window = limits.get(key)
                if not isinstance(window, dict):
                    continue
                pct = _num(window.get("used_percent"))
                if pct is None:
                    continue
                found = True   # Codex did report; an expired window still counts as a report
                reset_epoch = _num(window.get("resets_at"))
                if _window_expired(reset_epoch, clock):
                    continue
                minutes = _num(window.get("window_minutes")) or 0
                reset = _iso(reset_epoch)
                if minutes <= 24 * 60:
                    usage.five_hour_pct, usage.five_hour_resets_at = pct, reset
                else:
                    usage.seven_day_pct, usage.seven_day_resets_at = pct, reset
            if found:
                stamp = record.get("timestamp")
                usage.reported_at = stamp if isinstance(stamp, str) and stamp else None
                return usage
    usage.source = "unknown"
    usage.note = "Codex did not report its limits"
    return usage


# --------------------------------------------------------------------------- merge + write

def _merge(primary: ProviderUsage, backup: ProviderUsage) -> ProviderUsage:
    """Fill the gaps in `primary` from `backup` without ever overwriting a trusted number.
    Extra attributes a reader set on `primary` (`five_hour_as_of`, ...) ride along."""
    merged = ProviderUsage(provider=primary.provider)
    merged.__dict__.update(vars(primary))
    merged.provider = primary.provider or backup.provider
    if primary.source in ("unknown", "", None):
        merged.source = backup.source
        merged.note = backup.note
    for field in (
        "five_hour_pct", "seven_day_pct", "five_hour_resets_at", "seven_day_resets_at",
        "burn_pct_per_hour", "burn_cost_per_hour", "cost_today_usd", "tokens_today", "context_pct",
    ):
        if getattr(merged, field, None) is None:
            setattr(merged, field, getattr(backup, field, None))
    return merged


def read_hud(cfg) -> dict:
    """The last written `state/hud.json`, or an empty picture. Never raises."""
    try:
        with open(cfg.hud_file, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _append_sample(samples: list, stamp: Optional[str], pct: Optional[float]) -> list:
    """Add one reading unless it is missing or not newer than the last one kept. The stamp is
    when the SOURCE reported the number, not when the collector looked: an idle session's
    capture read again every minute is one reading, not a flat line that fakes `burn 0%/h`."""
    if pct is None or not stamp:
        return samples
    if samples:
        last = calc._as_epoch(samples[-1][0])
        this = calc._as_epoch(stamp)
        if last is not None and this is not None and this <= last:
            return samples
    return samples + [[stamp, pct]]


def _history(previous: dict, claude_pct: Optional[float], stamp: str,
             codex_pct: Optional[float] = None, codex_stamp: Optional[str] = None) -> dict:
    """Keep the last HISTORY_SAMPLES readings per provider: bare percentages for the sparkline,
    and the same readings with their timestamps so the burn rate can be measured over real
    elapsed time. Codex's series (`codex_pct`/`codex_samples`) is stamped with its log line's
    time; before 2026-09-26 nothing recorded one, which is why the Codex card never had a burn
    rate or a trend."""
    old = previous.get("history") or {}
    out: dict = {}
    for key, pct, when in (("claude", claude_pct, stamp), ("codex", codex_pct, codex_stamp)):
        samples = [s for s in (old.get(f"{key}_samples") or []) if isinstance(s, list) and len(s) == 2]
        samples = _append_sample(samples, when, pct)[-HISTORY_SAMPLES:]
        out[f"{key}_pct"] = [s[1] for s in samples]
        out[f"{key}_samples"] = samples
    return out


SLOW_FIELDS = ("cost_today_usd", "tokens_today")
SLOW_BURN_MAX_AGE_SECONDS = 600


def _carried_slow(previous: dict, provider: str) -> ProviderUsage:
    """The ccusage-side numbers of the last full pass, for a quick pass that skips ccusage
    (`collect(slow=False)`): today's cost only while it is still the same local day, the cost
    burn rate only while it is under ten minutes old. Anything else stays unknown."""
    usage = ProviderUsage(provider=provider, source="ccusage", note="from history",
                          fetched_at=utcnow_iso())
    old = previous.get(provider) if isinstance(previous.get(provider), dict) else {}
    slow_at = previous.get("slow_fetched_at") or previous.get("fetched_at")
    when = calc._as_epoch(slow_at)
    if when is None:
        return usage
    local_day = datetime.fromtimestamp(when).strftime("%Y-%m-%d")
    if local_day == _today_local():
        for field in SLOW_FIELDS:
            setattr(usage, field, old.get(field))
    if _now_epoch() - when <= SLOW_BURN_MAX_AGE_SECONDS:
        usage.burn_cost_per_hour = old.get("burn_cost_per_hour")
    return usage


def _fold_account(cfg, claude_live: ProviderUsage, poll_account: bool) -> None:
    """The account's own usage numbers as a second source behind the status line
. Only the collector process
    asks for a fresh read (`poll_account=True`, at most one request, on account.py's schedule);
    every other pass only folds in the reading the collector last saved. `[usage] account_read =
    false` skips all of it and leaves the picture exactly as it was before. Never raises."""
    try:
        from . import account
        if not account.enabled(cfg):
            return
        state = account.poll(cfg) if poll_account else account.load_state(cfg)
        account.apply(claude_live, state, _now_epoch())
    except Exception as exc:
        log.warning("account usage skipped: %s", type(exc).__name__)


def collect(cfg, write: bool = True, slow: bool = True, poll_account: bool = False) -> dict:
    """Gather every source, merge them, and write `state/hud.json`. `slow=True` (the collector
    process) also runs ccusage for cost and history; `slow=False` (the quick pass a reader runs
    when the collector is not running, `refresh_if_stale`) reads files only -- no subprocess, no
    fork -- and carries the ccusage numbers of the last full pass forward. `poll_account=True`
    (the collector process only, `collector.collect_pass`) may make the one account usage read
    its schedule allows; everyone else uses the collector's last saved reading."""
    stamp = utcnow_iso()
    previous = read_hud(cfg)

    if slow:
        claude_backup, codex_backup = claude_from_ccusage(cfg), codex_from_ccusage(cfg)
        slow_stamp = stamp
    else:
        claude_backup = _carried_slow(previous, "claude")
        codex_backup = _carried_slow(previous, "codex")
        slow_stamp = previous.get("slow_fetched_at") or previous.get("fetched_at")
    claude_live = claude_from_statusline(cfg.statusline_dir)
    _fold_account(cfg, claude_live, poll_account)
    claude = _merge(claude_live, claude_backup)
    codex = _merge(codex_rate_limits(cfg.codex_home), codex_backup)
    # Codex's reading time is its log line's; the same keys Claude carries, so a reader needs
    # one rule for "how old is this number" (calc.usage_as_of).
    codex.five_hour_as_of = codex.reported_at if codex.five_hour_pct is not None else None
    codex.seven_day_as_of = codex.reported_at if codex.seven_day_pct is not None else None

    history = _history(
        previous, claude.five_hour_pct, getattr(claude, "five_hour_as_of", None) or stamp,
        codex.five_hour_pct, codex.five_hour_as_of,
    )
    now = _now_epoch()
    claude.burn_pct_per_hour = calc.burn_pct_per_hour(
        [tuple(s) for s in history["claude_samples"]], now=now
    )
    codex.burn_pct_per_hour = calc.burn_pct_per_hour(
        [tuple(s) for s in history["codex_samples"]], now=now
    )
    # where each 5-hour window lands at its reset if the burn keeps up, with the
    # range the whole-number readings allow. None when there is no burn (nothing is drawn).
    try:
        from . import planning
        for key, usage in (("claude", claude), ("codex", codex)):
            usage.projection = planning.block_projection(
                usage.five_hour_pct, history[f"{key}_samples"], usage.five_hour_resets_at, now=now)
    except Exception as exc:
        log.warning("projection skipped: %s", exc)
    claude.fetched_at = stamp
    codex.fetched_at = stamp

    picture = {
        "fetched_at": stamp,
        "slow_fetched_at": slow_stamp,
        "writer": "collector" if slow else "quick pass",
        "claude": vars(claude).copy(),
        "codex": vars(codex).copy(),
        "history": history,
    }
    # The plan-percentage series + the batch line (hud/limits.py): a `sample` row whenever a
    # number moved, and the current work-the-plate batch summed for the BUDGET card. Fail-open:
    # a bad limits file must never cost the picture.
    try:
        from . import limits
        rows = limits.read_rows(cfg)
        for row in limits.sample_rows(picture, rows):
            if write:
                limits.append_row(cfg, row)
            rows.append(row)
        for provider in ("claude", "codex"):
            batch = limits.summarize(rows, provider)
            if batch:
                picture[provider]["batch"] = {k: v for k, v in batch.items() if k != "per_row"}
    except Exception as exc:
        log.warning("limits series skipped: %s", exc)
    if write:
        try:
            write_atomic(Path(cfg.hud_file), json.dumps(picture, indent=2, sort_keys=True))
        except OSError as exc:
            log.warning("could not write %s: %s", cfg.hud_file, exc)
    return picture


# --------------------------------------------------------------------------- the quick pass

# Past this age `state/hud.json` counts as unowned: the collector writes every 60 seconds, so
# two and a half intervals means it is not running (the budget window is closed). Kept above
# the collector's interval so a live collector and a reader never both write.
UNOWNED_AFTER_SECONDS = 150
QUICK_LOCK = "hud.quick-pass.lock"
QUICK_LOCK_STALE_SECONDS = 60


def refresh_if_stale(cfg, max_age_s: int = UNOWNED_AFTER_SECONDS,
                     now: Optional[float] = None) -> Optional[dict]:
    """Who writes `state/hud.json` when the budget window (and the collector in it) is closed:
    whoever reads it next. A reader that finds the file older than `max_age_s` runs one quick pass --
    `collect(slow=False)`: file reads only, no ccusage, no subprocess, so it is safe on the
    deck's own thread and in the tmux status command -- and returns the new picture. A fresh
    file, or another reader already mid-pass (a lock file), returns None and changes nothing.
    Never raises."""
    try:
        picture = read_hud(cfg)
        if not picture:
            # No file yet (first run) or an unreadable one: the collector's first full pass
            # makes it; a reader never invents a picture from nothing.
            return None
        age = calc.reading_age_seconds(picture.get("fetched_at"), now)
        if age is not None and age <= max_age_s:
            return None
        lock = Path(cfg.hud_file).with_name(QUICK_LOCK)
        try:
            if lock.exists() and _now_epoch() - lock.stat().st_mtime > QUICK_LOCK_STALE_SECONDS:
                lock.unlink()
        except OSError:
            pass
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return None                          # another reader is on it
        try:
            os.close(fd)
            return collect(cfg, slow=False)
        finally:
            try:
                lock.unlink()
            except OSError:
                pass
    except Exception as exc:
        log.warning("quick usage pass failed: %s", exc)
        return None
