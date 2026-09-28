"""The one usage line a Claude Code session reads before each prompt.

    [usage] 5-hour 47% used, about 75 model calls left at the last hour's pace, resets 21:40 ...

Where every number comes from (and nothing else -- this module never calls the account endpoint
and never reads a credential):
  - the percentages, their reset times and their age: `state/hud.json`, written by the usage
    collector (`hud/collector.py`);
  - how far the 5-hour bar moved over the last hour, and how the weekly bar moves against it:
    the collector's `sample` rows in `state/limits/limits.jsonl`;
  - how many model calls were made in that same stretch, and who made them: the session
    transcripts under `<claude_home>/projects/`, read only from where the last run stopped
    (offsets and per-call figures cached in `state/usage_line/cache.json`). Which transcripts
    are live comes from Pantheon's own hook log `state/agents/events.jsonl`, falling back to a
    folder scan when that log has gone quiet.

Words, chosen so a model reading them cannot mistake them:
  - "model call" = one assistant response from the API (one transcript `message.id`); a single
    prompt can run dozens of them. The line says "model calls", never "turns", because Claude
    would read "turns" as prompts and overestimate its room tenfold.
  - "calls left" = the window's remaining points divided by what one call (from any session on
    the account) cost over the measured stretch: points the bar moved / calls made in it. The
    bar moves in whole points, so fewer than MIN_POINTS of movement, or fewer than MIN_CALLS
    calls, and the line leaves calls out rather than divide noise.
  - "binding" = the window that runs out first at that pace, before its own reset. The weekly
    window's pace is the 5-hour pace times how far the weekly bar moved per 5-hour point over
    the last day of samples; unmeasured -> no weekly calls and no claim about which binds.
  - "this session's share" = its part of the last 15 minutes' spend across every session that
    spent in them, spend being tokens weighted by list-price ratios (input 1, cache write 1.25,
    cache read 0.1, output 5). Model price differences are NOT weighted (unverified which
    multiplier Fable carries), so a Sonnet session's share reads high next to an Opus one.
    Shown only when another session also spent; quiet sessions do not count.

A reading older than FRESH_SECONDS is shown with its age and without calls left (the headroom it
implies is out of date); older than TOO_OLD_SECONDS, nothing is printed at all.

`line` never raises; the hook prints what it returns (None = print nothing) and exits 0.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..models import format_clock, parse_ts

LOOKBACK_SECONDS = 3600          # "the last hour's pace"
SHARE_SECONDS = 15 * 60          # "the last 15 minutes' spend"
KEEP_SECONDS = LOOKBACK_SECONDS + 600   # per-call figures older than this are dropped from the cache
RATIO_SECONDS = 24 * 3600        # how far back the weekly-per-5-hour-point ratio looks
FRESH_SECONDS = 600              # same ten minutes as calc.STALE_READING_SECONDS
TOO_OLD_SECONDS = 3 * 3600
MIN_POINTS = 3                   # 5-hour points the bar must have moved to divide by
MIN_CALLS = 10
RATIO_MIN_FIVE = 10              # 5-hour points summed over the day before the ratio counts
RATIO_MIN_WEEKLY = 2
RESET_JITTER_SECONDS = 120       # the account's resets_at wobbles by a second between reads
SCAN_TTL_SECONDS = 120           # the fallback folder scan is reused this long
CHUNK = 256 * 1024
MAX_BACK_BYTES = 8 * 1024 * 1024
LIMITS_TAIL_BYTES = 512 * 1024   # ~1100 rows: over a day of five-minute samples plus marks

TRUSTED_SOURCES = ("account", "statusline")

WEIGHTS = {
    "input_tokens": 1.0,
    "cache_creation_input_tokens": 1.25,
    "cache_read_input_tokens": 0.1,
    "output_tokens": 5.0,
}

CACHE_VERSION = 1


# --------------------------------------------------------------------------- small helpers

def _epoch(ts) -> Optional[float]:
    if ts is None:
        return None
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    parsed = parse_ts(str(ts))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def _session_of(path: str) -> str:
    """`.../<sid>.jsonl` -> sid; `.../<sid>/subagents/agent-x.jsonl` -> sid (a sub-agent's spend
    is its parent session's spend)."""
    p = Path(path)
    if p.parent.name == "subagents":
        return p.parent.parent.name
    return p.stem


def _clock(epoch: float, clock_mode: str, with_day: bool = False) -> str:
    when = datetime.fromtimestamp(epoch, tz=timezone.utc)
    text = format_clock(when, clock_mode)
    if with_day:
        text = f"{when.astimezone().strftime('%a')} {text}"
    return text


def _about(n: float) -> str:
    """75 -> '75', 1234 -> '1,200': a count that is honest about being an estimate."""
    n = int(round(n))
    if n < 20:
        return str(max(0, n))
    if n < 100:
        return str(int(round(n / 5.0) * 5))
    digits = len(str(n)) - 2
    return f"{int(round(n, -digits)):,}"


def _age_words(seconds: float) -> str:
    total = int(seconds)
    if total < 3600:
        return f"{max(1, total // 60)}m"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


# --------------------------------------------------------------------------- file reading

def _complete_lines(data: bytes) -> tuple[list[str], int]:
    """Split on newlines; a trailing partial line (still being written) is left for next time.
    Returns the lines and how many bytes they covered."""
    end = data.rfind(b"\n")
    if end < 0:
        return [], 0
    return data[:end].decode("utf-8", errors="replace").splitlines(), end + 1


def read_new(path: str, offset: int) -> tuple[list[str], int]:
    """Complete lines from `offset` to the end. Raises OSError; the caller decides."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    lines, used = _complete_lines(data)
    return lines, offset + used


def read_back_to(path: str, cutoff: float, ts_of, size: Optional[int] = None) -> tuple[list[str], int]:
    """Complete lines from the first one at or after `cutoff` to the end, reading backward in
    chunks so a long file costs only its recent part. `ts_of(line)` -> epoch or None. Returns the
    lines and the end offset (just past the last complete line)."""
    if size is None:
        size = os.path.getsize(path)
    start = size
    buf = b""
    with open(path, "rb") as fh:
        while start > 0 and size - start < MAX_BACK_BYTES:
            step = min(CHUNK, start)
            start -= step
            fh.seek(start)
            buf = fh.read(step) + buf
            head = buf.split(b"\n", 1)
            probe = head[1] if start > 0 and len(head) > 1 else buf
            first_ts = None
            for raw in probe.split(b"\n"):
                if raw.strip():
                    first_ts = ts_of(raw.decode("utf-8", errors="replace"))
                    if first_ts is not None:
                        break
            if first_ts is not None and first_ts < cutoff:
                break
    if start > 0:
        cut = buf.find(b"\n")
        buf = buf[cut + 1:] if cut >= 0 else b""
    lines, used = _complete_lines(buf)
    return lines, (size - len(buf)) + used


def _json_ts(line: str, key: str) -> Optional[float]:
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return _epoch(rec.get(key)) if isinstance(rec, dict) else None


# --------------------------------------------------------------------------- transcripts

def _calls_in(lines: Iterable[str]) -> dict:
    """{message id: [epoch, weighted tokens]} for every real assistant response in `lines`.
    One API response is split over several records (one per content part) that repeat the same
    `message.id` and usage, so the id is the key, not the record."""
    out: dict = {}
    for line in lines:
        if '"assistant"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        if not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
            continue
        usage = msg.get("usage") or {}
        weight = 0.0
        for key, factor in WEIGHTS.items():
            try:
                weight += float(usage.get(key) or 0) * factor
            except (TypeError, ValueError):
                pass
        if weight <= 0:
            continue
        when = _epoch(rec.get("timestamp"))
        if when is None:
            continue
        mid = msg.get("id") or rec.get("requestId") or rec.get("uuid")
        if not mid:
            continue
        out[str(mid)] = [when, round(weight, 1)]
    return out


def _active_from_events(events_path: str, cache: dict, now: float) -> Optional[set]:
    """Main transcripts that logged a hook event in the lookback, from `events.jsonl`, read from
    where the last run stopped. None when the log is missing or has been quiet for the whole
    lookback."""
    try:
        st = os.stat(events_path)
    except OSError:
        return None
    if st.st_mtime < now - LOOKBACK_SECONDS:
        return None
    ev = cache.setdefault("events", {})
    active = ev.get("active") or {}
    offset = ev.get("offset")
    cutoff = now - KEEP_SECONDS
    if not isinstance(offset, int) or offset > st.st_size:
        # first run, or the log rotated (it rolls over at ~5 MB): read back to the cutoff
        lines, offset = read_back_to(events_path, cutoff, lambda s: _json_ts(s, "ts"), st.st_size)
    else:
        lines, offset = read_new(events_path, offset)
    for line in lines:
        if '"transcript_path"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        tp = rec.get("transcript_path") if isinstance(rec, dict) else None
        when = _epoch(rec.get("ts")) if isinstance(rec, dict) else None
        if tp and when and rec.get("source", "claude") == "claude":
            key = _norm(tp)
            if when > active.get(key, 0):
                active[key] = when
    active = {k: v for k, v in active.items() if v >= cutoff}
    ev["active"], ev["offset"] = active, offset
    return set(active)


def _scan_projects(projects_dir: str, cache: dict, now: float) -> set:
    """Every transcript (main or sub-agent) written in the lookback. Costs a few hundred ms on
    this box (thousands of files), so it is reused for SCAN_TTL_SECONDS."""
    scan = cache.get("scan") or {}
    if scan.get("at", 0) > now - SCAN_TTL_SECONDS and isinstance(scan.get("paths"), list):
        return set(scan["paths"])
    cutoff = now - KEEP_SECONDS
    found: set = set()
    try:
        projects = list(os.scandir(projects_dir))
    except OSError:
        projects = []
    for proj in projects:
        if not proj.is_dir():
            continue
        try:
            for entry in os.scandir(proj.path):
                if entry.name.endswith(".jsonl"):
                    try:
                        if entry.stat().st_mtime >= cutoff:
                            found.add(_norm(entry.path))
                    except OSError:
                        pass
        except OSError:
            continue
    cache["scan"] = {"at": now, "paths": sorted(found)}
    return found


def _with_subagents(mains: Iterable[str], cutoff: float) -> set:
    out: set = set()
    for main in mains:
        out.add(main)
        sub = Path(main).with_suffix("") / "subagents"
        try:
            for entry in os.scandir(sub):
                if entry.name.endswith(".jsonl"):
                    try:
                        if entry.stat().st_mtime >= cutoff:
                            out.add(_norm(entry.path))
                    except OSError:
                        pass
        except OSError:
            pass
    return out


def gather_calls(cfg_paths: dict, cache: dict, now: float, extra: Iterable[str] = ()) -> list:
    """[(epoch, weight, session id)] for every model call in the kept stretch, updating `cache`
    in place (offsets and per-file calls)."""
    cutoff = now - KEEP_SECONDS
    # The hook log names live sessions at once but misses any that run without Pantheon's hook
    #, so the folder scan (reused for two minutes)
    # is always added. Keys are lower-cased: Windows paths are case-blind and the same transcript
    # arrived as both `...-Documents-Projects` (hook log) and `...-documents-projects` (the folder).
    mains: dict = {}
    listed = list(_active_from_events(cfg_paths["events"], cache, now) or ())
    listed += list(_scan_projects(cfg_paths["projects"], cache, now))
    listed += [_norm(e) for e in extra if e]
    for p in listed:
        if Path(p).parent.name != "subagents":
            mains.setdefault(p.lower(), p)
    paths: dict = {}
    for p in _with_subagents(mains.values(), cutoff):
        paths.setdefault(p.lower(), p)
    files = cache.setdefault("files", {})
    for key, path in paths.items():
        entry = files.get(key) or {}
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        offset = entry.get("offset")
        calls = entry.get("calls") or {}
        try:
            if not isinstance(offset, int) or offset > size:
                lines, offset = read_back_to(path, cutoff, lambda s: _json_ts(s, "timestamp"), size)
                calls = {}
            elif offset == size:
                lines = []
            else:
                lines, offset = read_new(path, offset)
        except OSError:
            continue
        calls.update(_calls_in(lines))
        files[key] = {"offset": offset, "calls": calls}
    out = []
    for key in list(files):
        calls = {k: v for k, v in (files[key].get("calls") or {}).items() if v[0] >= cutoff}
        if not calls and key not in paths:
            del files[key]
            continue
        files[key]["calls"] = calls
        sid = _session_of(key)
        out.extend((v[0], v[1], sid) for v in calls.values())
    return out


# --------------------------------------------------------------------------- the bar's history

def read_samples(limits_path: str, now: float) -> list:
    """[(epoch, five_hour, five_resets, seven_day, seven_resets)] from the collector's sample
    rows over the last RATIO_SECONDS, oldest first."""
    try:
        size = os.path.getsize(limits_path)
        with open(limits_path, "rb") as fh:
            fh.seek(max(0, size - LIMITS_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return []
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        if '"sample"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("event") != "sample":
            continue
        if (rec.get("provider") or "claude") != "claude":
            continue
        when = _epoch(rec.get("utc"))
        if when is None or when < now - RATIO_SECONDS or when > now + 60:
            continue
        out.append((when, rec.get("five_hour"), _epoch(rec.get("five_hour_resets_at")),
                    rec.get("seven_day"), _epoch(rec.get("seven_day_resets_at"))))
    out.sort(key=lambda r: r[0])
    return out


def _same_window(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and abs(a - b) <= RESET_JITTER_SECONDS


def five_hour_tail(samples: list) -> Optional[tuple]:
    """(t0, p0, t1, p1) for the last hour of 5-hour readings, cut at the latest reset."""
    rows = [(t, float(p), r) for t, p, r, _, _ in samples if p is not None]
    if len(rows) < 2:
        return None
    t1, p1, r1 = rows[-1]
    t0, p0 = t1, p1
    for t, p, r in reversed(rows[:-1]):
        if t < t1 - LOOKBACK_SECONDS or p > p0 or not _same_window(r, r1):
            break
        t0, p0 = t, p
    if t0 >= t1:
        return None
    return t0, p0, t1, p1


def weekly_ratio(samples: list) -> Optional[float]:
    """Weekly points per 5-hour point over the last day: consecutive sample pairs inside the same
    two windows, both bars moving up or not at all."""
    sum5 = sumw = 0.0
    for a, b in zip(samples, samples[1:]):
        _, f0, r0, w0, s0 = a
        _, f1, r1, w1, s1 = b
        if None in (f0, f1, w0, w1):
            continue
        if not (_same_window(r0, r1) and _same_window(s0, s1)):
            continue
        d5, dw = float(f1) - float(f0), float(w1) - float(w0)
        if d5 < 0 or dw < 0:
            continue
        sum5 += d5
        sumw += dw
    if sum5 < RATIO_MIN_FIVE or sumw < RATIO_MIN_WEEKLY:
        return None
    return sumw / sum5


# --------------------------------------------------------------------------- the line

def _trusted(block: dict, field: str) -> bool:
    src = block.get(f"{field}_source") or block.get("source")
    return src in TRUSTED_SOURCES


def compose(hud: dict, samples: list, calls: list, session_id: Optional[str], now: float,
            clock_mode: str = "24h") -> Optional[str]:
    """The line, from already-read inputs (pure: the tests drive this directly)."""
    claude = (hud or {}).get("claude") or {}
    five = claude.get("five_hour_pct") if _trusted(claude, "five_hour") else None
    week = claude.get("seven_day_pct") if _trusted(claude, "seven_day") else None
    five_reset = _epoch(claude.get("five_hour_resets_at"))
    week_reset = _epoch(claude.get("seven_day_resets_at"))
    as_of = _epoch(claude.get("five_hour_as_of") or claude.get("seven_day_as_of")
                   or claude.get("reported_at"))
    if as_of is None or (five is None and week is None):
        return None
    age = max(0.0, now - as_of)
    if age > TOO_OLD_SECONDS:
        return None
    fresh = age <= FRESH_SECONDS
    if five_reset is not None and five_reset <= now:
        five = None                    # the window this reading belongs to is over
        five_note = f"5-hour reset at {_clock(five_reset, clock_mode)}, no reading since"
    else:
        five_note = None
    if week_reset is not None and week_reset <= now:
        week = None

    # pace: 5-hour points per model call over the measured stretch
    per_call = rate = None
    tail = five_hour_tail(samples)
    if fresh and tail:
        t0, p0, t1, p1 = tail
        moved = p1 - p0
        n = sum(1 for t, _, _ in calls if t0 < t <= t1)
        if moved >= MIN_POINTS and n >= MIN_CALLS:
            per_call = moved / n
            rate = n / ((t1 - t0) / 3600.0)
    ratio = weekly_ratio(samples) if per_call else None

    windows = []   # (name, pct, reset, calls_left or None, runs_out_epoch or None)
    if five is not None:
        left = (100.0 - float(five)) / per_call if per_call else None
        out = now + left / rate * 3600 if left is not None and rate else None
        windows.append(("5-hour", float(five), five_reset, left, out))
    if week is not None:
        left = (100.0 - float(week)) / (per_call * ratio) if per_call and ratio else None
        out = now + left / rate * 3600 if left is not None and rate else None
        windows.append(("weekly", float(week), week_reset, left, out))

    binding = None
    running_out = [w for w in windows if w[4] is not None and (w[2] is None or w[4] < w[2])]
    if running_out:
        binding = min(running_out, key=lambda w: w[4])[0]
        windows.sort(key=lambda w: w[0] != binding)

    parts = []
    for name, pct, reset, left, out in windows:
        bits = [f"{name} {pct:.0f}% used"]
        if left is not None:
            bits.append(f"about {_about(left)} model calls left at the last hour's pace")
        when = (_clock(reset, clock_mode, with_day=name == "weekly" and reset - now > 86400)
                if reset is not None else None)
        if name == binding:
            tail = f", before its {when} reset" if when else ""
            bits.append(f"runs out about {_clock(out, clock_mode)}{tail}")
        elif when:
            bits.append(f"resets {when}")
        text = ", ".join(bits)
        if name == binding and all(w[3] is not None for w in windows):
            text = "binding: " + text     # only once every window shown has been judged
        parts.append(text)
    if five_note:
        parts.insert(0, five_note)
    if per_call and not running_out and any(w[3] is not None for w in windows):
        judged = [w[0] for w in windows if w[3] is not None]
        if len(judged) == 2:
            parts.append("at this pace neither window runs out before it resets")
        else:
            parts.append(f"at this pace the {judged[0]} window lasts to its reset")

    share = session_share(calls, session_id, now)
    if share:
        parts.append(share)
    if not parts:
        return None
    head = "[usage] " if fresh else f"[usage] reading {_age_words(age)} old: "
    return head + " · ".join(parts)


def session_share(calls: list, session_id: Optional[str], now: float) -> Optional[str]:
    if not session_id:
        return None
    spend: dict = {}
    for t, w, sid in calls:
        if now - SHARE_SECONDS <= t <= now + 60:
            spend[sid] = spend.get(sid, 0.0) + w
    others = [s for s in spend if s != session_id and spend[s] > 0]
    if not others:
        return None
    total = sum(spend.values())
    mine = spend.get(session_id, 0.0) / total * 100 if total else 0.0
    if mine <= 0:
        n = len(others)
        return (f"{n} other session{'s' if n != 1 else ''} spent in the last 15 min, "
                f"this one nothing yet")
    shown = "under 1" if 0 < mine < 1 else f"{mine:.0f}"
    return (f"this session ~{shown}% of the last 15 min's spend "
            f"({len(others)} other session{'s' if len(others) != 1 else ''} spending)")


# --------------------------------------------------------------------------- the whole run

def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


def enabled(cfg) -> bool:
    """`PANTHEON_USAGE_LINE` (0/false/off or 1/true/on) beats `[usage] prompt_line`."""
    env = os.environ.get("PANTHEON_USAGE_LINE", "").strip().lower()
    if env in ("0", "false", "off", "no"):
        return False
    if env in ("1", "true", "on", "yes"):
        return True
    try:
        return bool(cfg.usage_settings().prompt_line)
    except Exception:
        return True


def line(cfg, payload: Optional[dict] = None, now: Optional[float] = None) -> Optional[str]:
    """Everything the hook does except printing. Never raises."""
    try:
        if not enabled(cfg):
            return None
        now = time.time() if now is None else now
        payload = payload or {}
        state = Path(cfg.state_dir)
        hud = _load_json(Path(cfg.hud_file))
        if not hud:
            return None
        cache_path = state / "usage_line" / "cache.json"
        cache = _load_json(cache_path)
        if cache.get("v") != CACHE_VERSION:
            cache = {"v": CACHE_VERSION}
        paths = {
            "events": str(cfg.events_file),
            "projects": str(Path(cfg.claude_home) / "projects"),
        }
        calls = gather_calls(paths, cache, now, extra=[payload.get("transcript_path") or ""])
        _save_cache(cache_path, cache)
        samples = read_samples(str(state / "limits" / "limits.jsonl"), now)
        clock_mode = "24h"
        try:
            clock_mode = cfg.appearance_settings().clock or "24h"
        except Exception:
            pass
        return compose(hud, samples, calls, payload.get("session_id"), now, clock_mode)
    except Exception:
        if os.environ.get("PANTHEON_USAGE_LINE_DEBUG"):
            import traceback
            traceback.print_exc()
        return None
