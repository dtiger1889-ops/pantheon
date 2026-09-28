"""The plan-percentage time series and the batch burn arithmetic.

The usage-probe arithmetic, in Python, plus the sums a hand-run probe left to a human. One file, `state/limits/limits.jsonl`, one JSON object per line, in the
feed shape the seed rows in `tests/fixtures/calibration/` use. Three kinds of row share it:

  - `sample`  -- the usage collector appends one whenever a provider's 5-hour or weekly percent
                 (or its reset time) changes between passes, so the bar's movement is on disk;
  - `start`   -- a work-the-plate batch begins (`bin/usage_mark start`);
  - `before` / `after` -- one pair per delegated row, `after` carrying the worker's tokens and wall;
                 or one `before --batch` for workers launched together, every `after` without a
                 `before` of its own pairing against it (see `_match`).

`summarize` turns the rows since the last `start` into the batch line on the BUDGET card: rows done,
5-hour points burned, weekly points burned, tokens, and points per 100k tokens -- the number the
calibration constants re-fit from. A pair whose two readings sit in different 5-hour windows (the
reset time changed) is counted as tokens but never as points: a delta across a reset is not a
delta. Percentages are integer resolution, so a single row's points are noise; the batch total is
the number to read.

Nothing here raises at the caller and nothing here shells out or touches a network.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

from ..models import utcnow_iso
from . import sources

FILE_NAME = "limits.jsonl"
MARK_EVENTS = ("start", "before", "after")
SAMPLE_EVENT = "sample"
FIELDS = (
    "utc", "event", "provider", "repo", "five_hour", "five_hour_resets_at", "seven_day",
    "seven_day_resets_at", "statusline_written_at", "model", "session_cost_usd", "tokens",
    "duration_ms", "note", "size",
)
# `size`: the dispatched queue row's `est_context` (small / medium / large), set
# on an `after` mark with `usage_mark after --size medium`, so the planning page can learn how
# many tokens each size of row really takes. Older rows have none; they still count for points.


# --------------------------------------------------------------------------- the file

def limits_file(cfg) -> Path:
    return Path(cfg.state_dir) / "limits" / FILE_NAME


def read_rows(cfg) -> list[dict]:
    """Every parseable row, in file order. A missing file or a bad line is skipped, never raised."""
    path = limits_file(cfg)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_row(cfg, row: dict) -> None:
    path = limits_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=False) + "\n")


def _row(**values) -> dict:
    row = {name: None for name in FIELDS}
    row.update({"event": "", "provider": "claude", "repo": "", "tokens": 0, "duration_ms": 0, "note": ""})
    row.update(values)
    if not row.get("utc"):
        row["utc"] = utcnow_iso()
    return row


def _provider(row: dict) -> str:
    """The seed rows predate the field; they are all Claude readings."""
    return str(row.get("provider") or "claude")


def _num(value) -> Optional[float]:
    return sources._num(value)


# --------------------------------------------------------------------------- samples (collector)

def sample_rows(picture: dict, rows: Iterable[dict]) -> list[dict]:
    """The `sample` rows one collector pass should append: one per provider whose 5-hour or weekly
    percent, or either reset time, differs from that provider's newest row of any kind. Nothing to
    report (no numbers, or nothing moved) -> an empty list, so a quiet hour adds no lines."""
    latest: dict[str, dict] = {}
    for row in rows or ():
        latest[_provider(row)] = row
    out: list[dict] = []
    stamp = picture.get("fetched_at") or utcnow_iso()
    for provider in ("claude", "codex"):
        usage = picture.get(provider) or {}
        if not isinstance(usage, dict):
            continue
        five, week = _num(usage.get("five_hour_pct")), _num(usage.get("seven_day_pct"))
        if five is None and week is None:
            continue
        five_reset = usage.get("five_hour_resets_at") or None
        week_reset = usage.get("seven_day_resets_at") or None
        last = latest.get(provider)
        if last is not None and (
            _num(last.get("five_hour")) == five and _num(last.get("seven_day")) == week
            and (last.get("five_hour_resets_at") or None) == five_reset
            and (last.get("seven_day_resets_at") or None) == week_reset
        ):
            continue
        out.append(_row(
            utc=stamp, event=SAMPLE_EVENT, provider=provider, five_hour=five,
            five_hour_resets_at=five_reset, seven_day=week, seven_day_resets_at=week_reset,
            statusline_written_at=usage.get("reported_at") or None, model=None,
            session_cost_usd=_num(usage.get("cost_today_usd")),
            note=f"collector; source {usage.get('source') or '?'}",
        ))
    return out


# --------------------------------------------------------------------------- marks (usage_mark)

def _capture_for(cfg, session_id: Optional[str]) -> tuple[dict, Optional[str], str]:
    """The statusline capture to read a mark from: this session's own file when the id is known,
    else the newest fresh capture on disk. Returns (data, written_at_iso, how)."""
    if session_id:
        data = sources.statusline_for(cfg.statusline_dir, session_id)
        if data:
            path = Path(cfg.statusline_dir) / f"{session_id}.json"
            try:
                written = sources._iso(path.stat().st_mtime)
            except OSError:
                written = None
            return data, written, "own session"
    files = sources.read_statusline_files(cfg.statusline_dir)
    if files:
        return files[0], None, "newest live capture (no capture for this session)"
    return {}, None, "no statusline capture"


def mark(cfg, event: str, repo: str = "", tokens: int = 0, duration_ms: int = 0, note: str = "",
         session_id: Optional[str] = None, provider: str = "claude", now: Optional[str] = None,
         write: bool = True, batch: bool = False, size: Optional[str] = None) -> dict:
    """Append one `start` / `before` / `after` row from the live numbers and return it.

    Claude: the percentages come from the statusline capture (`CLAUDE_CODE_SESSION_ID` names the
    session; a Desktop-app session has none, so the row says so in `note` and carries nulls --
    tokens still count). Codex: from its own session log. Never raises.

    `batch=True` (only on a `before`) marks ONE reading for workers launched together: every
    following `after` without a `before` of its own pairs against it, and the batch reports the
    bar's total movement instead of per-row points."""
    if event not in MARK_EVENTS:
        raise ValueError(f"event must be one of {MARK_EVENTS}, not {event!r}")
    if batch and event != "before":
        raise ValueError("--batch goes on a `before` mark only")
    session_id = session_id if session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    notes = [note] if note else []
    if provider == "codex":
        usage = sources.codex_rate_limits(cfg.codex_home)
        row = _row(
            utc=now, event=event, provider="codex", repo=repo,
            five_hour=usage.five_hour_pct, five_hour_resets_at=usage.five_hour_resets_at,
            seven_day=usage.seven_day_pct, seven_day_resets_at=usage.seven_day_resets_at,
            statusline_written_at=usage.reported_at, tokens=int(tokens or 0),
            duration_ms=int(duration_ms or 0),
        )
        if usage.source == "unknown":
            notes.append(usage.note or "Codex did not report its limits")
    else:
        data, written, how = _capture_for(cfg, session_id)
        rl = data.get("rate_limits") if isinstance(data, dict) else None
        rl = rl if isinstance(rl, dict) else {}
        cost = _num(sources._dig(data, "cost", "total_cost_usd"))
        row = _row(
            utc=now, event=event, provider="claude", repo=repo,
            five_hour=_num(sources._dig(rl, "five_hour", "used_percentage")),
            five_hour_resets_at=sources._iso(_num(sources._dig(rl, "five_hour", "resets_at"))),
            seven_day=_num(sources._dig(rl, "seven_day", "used_percentage")),
            seven_day_resets_at=sources._iso(_num(sources._dig(rl, "seven_day", "resets_at"))),
            statusline_written_at=written, model=sources._dig(data, "model", "id"),
            session_cost_usd=(round(cost, 2) if cost is not None else None),
            tokens=int(tokens or 0), duration_ms=int(duration_ms or 0),
        )
        if how != "own session":
            notes.append(how)
    row["note"] = "; ".join(n for n in notes if n)
    if size:
        row["size"] = str(size).strip().lower()
    if batch:
        row["batch"] = True
    if write:
        append_row(cfg, row)
    return row


# --------------------------------------------------------------------------- arithmetic

def current_batch(rows: Iterable[dict]) -> list[dict]:
    """The rows from the last `start` onward (inclusive). No `start` yet -> empty."""
    rows = list(rows or ())
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("event") == "start":
            return rows[i:]
    return []


def _match(rows: Iterable[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[tuple[Optional[dict], dict]]]:
    """Match `before`/`after` marks. Returns (pairs, still-pending befores, batch afters).

    A `before` pairs with the next `after` of the same (provider, repo), first in first out, so
    five befores on one repo pair with five afters instead of overwriting each other. An `after` with no `before` of its
    own is a BATCH after: it belongs to the provider's batch anchor -- the newest `before --batch`
    row, else the batch's `start` row -- and its points can only ever be the batch total
.
    The anchor is None when no batch was started for that provider; its tokens still count."""
    pending: dict[tuple[str, str], list[dict]] = {}
    anchor: dict[str, dict] = {}
    pairs: list[tuple[dict, dict]] = []
    batched: list[tuple[Optional[dict], dict]] = []
    for row in rows or ():
        event = row.get("event")
        provider = _provider(row)
        if event == "start":
            anchor = {provider: row}
            continue
        key = (provider, str(row.get("repo") or ""))
        if event == "before":
            if row.get("batch"):
                anchor[provider] = row
            else:
                pending.setdefault(key, []).append(row)
        elif event == "after":
            queue = pending.get(key)
            if queue:
                pairs.append((queue.pop(0), row))
            else:
                batched.append((anchor.get(provider), row))
    still = [b for queue in pending.values() for b in queue]
    still.sort(key=lambda r: str(r.get("utc") or ""))
    return pairs, still, batched


def _pairs(rows: Iterable[dict]) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """`before`/`after` rows matched by (provider, repo) in file order. Returns (pairs, still
    pending befores). Batch afters (no `before` of their own) are left out; see `_match`."""
    pairs, pending, _ = _match(rows)
    return pairs, pending


def _delta(before: dict, after: dict, field: str, reset_field: str) -> tuple[Optional[float], bool]:
    """(points burned, crossed a window reset). None when either reading is missing."""
    a, b = _num(after.get(field)), _num(before.get(field))
    if a is None or b is None:
        return None, False
    if (before.get(reset_field) or None) != (after.get(reset_field) or None):
        return None, True
    return a - b, False


def _overlapped(before: dict, after: dict, rows: Iterable[dict]) -> bool:
    """True when another mark of the same provider fell between this pair's two readings: the
    agents ran in parallel, so the bar's movement in that span belongs to all of them and this
    pair's points would be counted again by its neighbours."""
    b, a = str(before.get("utc") or ""), str(after.get("utc") or "")
    if not b or not a:
        return False
    for row in rows or ():
        if row is before or row is after or _provider(row) != _provider(after):
            continue
        if row.get("event") not in ("before", "after"):
            continue
        u = str(row.get("utc") or "")
        if b < u < a:
            return True
    return False


def pair_stats(before: dict, after: dict, rows: Iterable[dict] = ()) -> dict:
    five, five_reset = _delta(before, after, "five_hour", "five_hour_resets_at")
    week, week_reset = _delta(before, after, "seven_day", "seven_day_resets_at")
    overlapped = _overlapped(before, after, rows)
    if overlapped:
        five = week = None          # tokens still count; the points belong to the batch total
    return {
        "overlapped": overlapped,
        "repo": after.get("repo") or before.get("repo") or "",
        "provider": _provider(after),
        "tokens": int(_num(after.get("tokens")) or 0),
        "duration_ms": int(_num(after.get("duration_ms")) or 0),
        "five_hour_points": five,
        "seven_day_points": week,
        "crossed_reset": bool(five_reset or week_reset),
    }


def _batch_after_stats(after: dict) -> dict:
    """A batch after (no `before` of its own): tokens and wall count, points are the batch's."""
    return {
        "overlapped": False,
        "batch": True,
        "repo": after.get("repo") or "",
        "provider": _provider(after),
        "tokens": int(_num(after.get("tokens")) or 0),
        "duration_ms": int(_num(after.get("duration_ms")) or 0),
        "five_hour_points": None,
        "seven_day_points": None,
        "crossed_reset": False,
    }


def _per_100k(points: Optional[float], tokens: int) -> Optional[float]:
    if points is None or tokens <= 0:
        return None
    return points / (tokens / 100_000.0)


def summarize(rows: Iterable[dict], provider: str = "claude") -> Optional[dict]:
    """The batch picture for ONE provider's BUDGET card (Claude's and Codex's bars are different
    allowances, so their points are never added together). None when no batch has been started,
    or when the batch has no rows of this provider yet."""
    batch = current_batch(rows)
    if not batch:
        return None
    start = batch[0]
    pairs, pending, batched = _match(batch)
    pairs = [(b, a) for b, a in pairs if _provider(a) == provider]
    pending = [b for b in pending if _provider(b) == provider]
    batched = [a for _, a in batched if _provider(a) == provider]
    if provider != "claude" and not pairs and not pending and not batched:
        return None
    stats = [pair_stats(b, a, batch) for b, a in pairs] + [_batch_after_stats(a) for a in batched]
    reliable = [s for s in stats if s["five_hour_points"] is not None]
    tokens = sum(s["tokens"] for s in stats)
    reliable_tokens = sum(s["tokens"] for s in reliable)
    five_points = sum(s["five_hour_points"] for s in reliable) if reliable else None
    week_stats = [s for s in stats if s["seven_day_points"] is not None]
    week_points = sum(s["seven_day_points"] for s in week_stats) if week_stats else None
    # The batch ends at its last mark, not at the newest collector sample: once every agent has
    # reported, later samples belong to whatever ran next.
    marks = [r for r in batch if r.get("event") in MARK_EVENTS]
    newest = marks[-1] if marks else start
    bar_moved, _ = _delta(start, newest, "five_hour", "five_hour_resets_at")
    week_moved, _ = _delta(start, newest, "seven_day", "seven_day_resets_at")
    parallel = sum(1 for s in stats if s["overlapped"] or s.get("batch"))
    if parallel:
        # Once any row's points belong to the batch total, every row's do: the one worker that
        # happened to pair cleanly ran beside the others too.
        parallel = len(stats)
    if parallel and not pending:
        # Agents ran together: the only honest points are the bar's own movement from `start`
        # to the newest reading (the orchestrator's own turns are inside it, as in the benchmark).
        five_points, week_points, reliable_tokens = bar_moved, week_moved, tokens
    return {
        "provider": provider,
        "since": start.get("utc"),
        "rows": len(stats),
        "running": len(pending),
        "tokens": tokens,
        "five_hour_points": five_points,
        "seven_day_points": week_points,
        "points_per_100k": _per_100k(five_points, reliable_tokens),
        "parallel": parallel,
        "crossed_reset": sum(1 for s in stats if s["crossed_reset"]),
        "unmeasured": len(stats) - len(reliable),
        "bar_moved": bar_moved,
        "per_row": stats,
    }


def _trailing_pairs(seg: list[dict]) -> tuple[list[tuple[dict, dict]], set[int]]:
    """Clean before/after pairs that start after every other mark of the segment: they ran once the
    batch was over, so they are serial rows, not part of it. Returns (pairs, ids of their rows).
    2026-09-26: a lone reviewer pair marked without a new `start` landed two days after the
    2026-09-24 batch; the batch's end moved to it across a window reset, so the batch (1.7M tokens)
    and the pair both fell out of the refit. A pair that starts before some other mark still ran beside the batch and stays in it."""
    pairs, _, _ = _match(seg)
    marks = [r for r in seg if r.get("event") in ("before", "after")]
    tail: list[tuple[dict, dict]] = []
    tail_ids: set[int] = set()
    for b, a in sorted(pairs, key=lambda p: str(p[0].get("utc") or ""), reverse=True):
        start = str(b.get("utc") or "")
        later = [r for r in marks if r is not b and r is not a and id(r) not in tail_ids
                 and str(r.get("utc") or "") >= start]
        if later or not start:
            break
        tail.append((b, a))
        tail_ids.update((id(b), id(a)))
    tail.reverse()
    return tail, tail_ids


def _segments(rows: list[dict]) -> list[list[dict]]:
    """The file cut at every `start`: the rows before the first `start`, then one list per batch."""
    out: list[list[dict]] = [[]]
    for row in rows:
        if row.get("event") == "start":
            out.append([])
        out[-1].append(row)
    return [seg for seg in out if seg]


def refit(rows: Iterable[dict], provider: str = "claude", detail: bool = False) -> dict:
    """Points per 100k tokens over the whole file (not just this batch), in two lanes that are never
    mixed, because concurrent batches cost several times what one-at-a-time workers do:

      - serial: every reliable before/after pair of one provider -- the number
        the seed figures were hand-derived from, recomputed. A pair inside a batch whose points
        are the batch total (agents ran together) never counts here;
      - batch:  each such batch counted ONCE, as `summarize` reports it -- the bar's movement from
        its `start` to its last mark over all its workers' tokens. Only batches with no worker still
        running, both readings present and no window reset in between.

    `detail=True` adds the per-row and per-batch ratios the pooled figures are made of
    (`serial_five_hour_ratios`, ... each points per 100k tokens), which the planning page
    reads its P10..P90 range from. The pooled figures themselves are unchanged."""
    rows = list(rows or ())
    serial: list[dict] = []
    batches: list[dict] = []
    for seg in _segments(rows):
        if seg[0].get("event") == "start":
            tail, tail_ids = _trailing_pairs(seg)
            summary = summarize([r for r in seg if id(r) not in tail_ids], provider)
            if summary and summary.get("parallel"):
                if (not summary.get("running") and summary.get("tokens")
                        and summary.get("bar_moved") is not None):
                    batches.append(summary)
                serial += [pair_stats(b, a, seg) for b, a in tail if _provider(a) == provider]
                continue
        pairs, _ = _pairs(seg)
        serial += [pair_stats(b, a, seg) for b, a in pairs if _provider(a) == provider]
    five = [s for s in serial if s["five_hour_points"] is not None and s["tokens"] > 0]
    week = [s for s in serial if s["seven_day_points"] is not None and s["tokens"] > 0]
    five_tokens = sum(s["tokens"] for s in five)
    week_tokens = sum(s["tokens"] for s in week)
    batch_tokens = sum(b["tokens"] for b in batches)
    week_batches = [b for b in batches if b.get("seven_day_points") is not None]
    week_batch_tokens = sum(b["tokens"] for b in week_batches)
    out = {
        "provider": provider,
        "pairs": len(serial),
        "five_hour_pairs": len(five),
        "five_hour_points_per_100k": _per_100k(sum(s["five_hour_points"] for s in five), five_tokens) if five else None,
        "seven_day_pairs": len(week),
        "seven_day_points_per_100k": _per_100k(sum(s["seven_day_points"] for s in week), week_tokens) if week else None,
        "tokens_measured": five_tokens,
        "batches": len(batches),
        "batch_five_hour_points_per_100k": _per_100k(sum(b["five_hour_points"] for b in batches), batch_tokens) if batches else None,
        "batch_seven_day_points_per_100k": (
            _per_100k(sum(b["seven_day_points"] for b in week_batches), week_batch_tokens) if week_batches else None),
        "batch_tokens_measured": batch_tokens,
    }
    if detail:
        out["serial_five_hour_ratios"] = [_per_100k(s["five_hour_points"], s["tokens"]) for s in five]
        out["serial_seven_day_ratios"] = [_per_100k(s["seven_day_points"], s["tokens"]) for s in week]
        out["batch_five_hour_ratios"] = [_per_100k(b["five_hour_points"], b["tokens"]) for b in batches]
        out["batch_seven_day_ratios"] = [_per_100k(b["seven_day_points"], b["tokens"]) for b in week_batches]
    return out


def _fmt_points(value: Optional[float], unit: str) -> str:
    if value is None:
        return f"? {unit}"
    return f"{value:+.0f} {unit}"


def batch_text(summary: Optional[dict], dot: str = "·") -> Optional[str]:
    """`3 rows · +4 pts 5h · +1 wk · 490k tok · 1.1 pts/100k` -- or `2 rows (1 running) …`.
    None when there is no batch. Word order is the order the user reads it: how much of the 5-hour
    window the batch has cost is the number that matters."""
    if not summary:
        return None
    rows = summary.get("rows") or 0
    running = summary.get("running") or 0
    head = f"{rows} row{'s' if rows != 1 else ''}" + (f" ({running} running)" if running else "")
    tokens = summary.get("tokens") or 0
    parts = [
        head,
        _fmt_points(summary.get("five_hour_points"), "pts 5h"),
        _fmt_points(summary.get("seven_day_points"), "wk"),
        f"{tokens / 1000:.0f}k tok",
    ]
    rate = summary.get("points_per_100k")
    if rate is not None:
        parts.append(f"{rate:.1f} pts/100k")
    if (summary.get("parallel") or 0) > 1:
        parts.append(f"{summary['parallel']} ran in parallel, points are the batch total")
    elif summary.get("parallel"):
        parts.append("points are the batch total")
    if summary.get("crossed_reset"):
        parts.append(f"{summary['crossed_reset']} crossed a reset")
    return f" {dot} ".join(parts)


# --------------------------------------------------------------------------- command line

def main(argv: Optional[list[str]] = None) -> int:
    """`python -m pantheon.hud.limits start|before|after [--repo X] [--tokens N] [--duration-ms M]
    [--note ...] [--provider claude|codex]`, `... summary`, `... refit`. Prints JSON."""
    from .. import config as config_mod

    parser = argparse.ArgumentParser(prog="usage_mark")
    parser.add_argument("event", choices=(*MARK_EVENTS, "summary", "refit"))
    parser.add_argument("--repo", default="")
    parser.add_argument("--tokens", type=int, default=0)
    parser.add_argument("--duration-ms", type=int, default=0)
    parser.add_argument("--note", default="")
    parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--size", choices=("small", "medium", "large"), default=None,
                        help="on `after`: the dispatched row's est_context, so plans learn its real tokens")
    parser.add_argument("--batch", action="store_true",
                        help="on `before`: one reading for several workers launched together")
    args = parser.parse_args(argv)
    if args.batch and args.event != "before":
        parser.error("--batch goes on a `before` mark only")
    cfg = config_mod.load()
    if args.event == "summary":
        summary = summarize(read_rows(cfg), args.provider)
        out = dict(summary or {"rows": 0, "note": "no batch started; run `usage_mark start` first"})
        out.pop("per_row", None)
        out["line"] = batch_text(summary)
    elif args.event == "refit":
        out = refit(read_rows(cfg), args.provider)
    else:
        out = mark(cfg, args.event, repo=args.repo, tokens=args.tokens, duration_ms=args.duration_ms,
                   note=args.note, session_id=args.session_id, provider=args.provider, batch=args.batch,
                   size=args.size)
    print(json.dumps(out, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
