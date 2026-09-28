"""The planning page: pick queue rows, see what they
would cost of the 5-hour and weekly windows before any of them is dispatched.

`python -m pantheon.planning` (or `bin/plan`) opens it as its own window. Keys: up/down move,
Space adds or removes the row under the cursor (the plan keeps the order you added them in),
`t` switches between workers launched together and one at a time, `c` switches Claude / Codex, `r`
re-reads the queue and the limits file, `q` closes.

Every figure comes from `pantheon/planning/fit.py` re-fitting `state/limits/limits.jsonl` on each
read, so a row that has actually run moves the next plan (spec step 5). Every figure shows its
range and how many past rows stand behind it; `not enough data` where that is the truth. Percent
only: dollars stay off until the user asks for them.

Not wired: `Dispatch this plan`. The row is shown greyed; rows are dispatched from the queue.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static

from .. import config, theme
from ..hud import limits, sources
from ..hud import planning as P
from ..models import TaskRow
from . import fit

log = logging.getLogger("pantheon.planning")

PHONE_AT = 90             # at or under this many columns: one line per row, no weekly column
DONE = {"done", "dropped", "cancelled"}
PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex"}


# --------------------------------------------------------------------------- the model

@dataclass
class Beliefs:
    """What the page is costing from, read once per refresh."""

    tokens: dict
    rates: dict                      # lane -> {(provider, window): Rate}
    workers: int = 0                 # finished workers on file (token spread)
    current: dict = field(default_factory=dict)   # provider -> {"five_hour": pct, "seven_day": pct}

    @classmethod
    def from_rows(cls, rows: list[dict], picture: Optional[dict] = None) -> "Beliefs":
        picture = picture or {}
        current = {}
        for provider in fit.PROVIDERS:
            usage = picture.get(provider) if isinstance(picture.get(provider), dict) else {}
            current[provider] = {"five_hour": usage.get("five_hour_pct"),
                                 "seven_day": usage.get("seven_day_pct")}
        return cls(
            tokens=fit.token_table(rows),
            rates={lane: fit.per_100k(rows, lane) for lane in P.LANES},
            workers=len(fit._finished_workers(rows)),
            current=current,
        )


def open_rows(rows: list[TaskRow]) -> list[TaskRow]:
    """The rows worth planning: not done. The Agent's plate first, then the rest in source order."""
    live = [r for r in rows or () if not r.done and (r.status or "").lower() not in DONE]
    return sorted(live, key=lambda r: 0 if r.agent else 1)


def size_word(row: TaskRow) -> str:
    text = (row.est_context or "").strip().lower()
    return text if text in P.SIZES else "unsized"


def estimates(rows: list[TaskRow], beliefs: Beliefs, provider: str, lane: str) -> dict:
    """window -> (per-row Estimates in plan order, the plan total)."""
    out = {}
    rates = beliefs.rates.get(lane) or {}
    for window in P.WINDOWS:
        per_row = [P.row_estimate(r.est_context, provider, window, beliefs.tokens, rates) for r in rows]
        out[window] = (per_row, P.plan_totals(per_row))
    return out


def _shorten(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    if width <= 1:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def _basis_line(beliefs: Beliefs, provider: str, lane: str, short: bool = False) -> str:
    """Where the numbers come from, in one sentence: the lane, the rows behind the cost, the rows
    behind the token counts, and anything not measured."""
    who = PROVIDER_NAMES.get(provider, provider)
    rate = (beliefs.rates.get(lane) or {}).get((provider, "five_hour")) or P.Rate(None)
    unit = "past batches" if lane == P.TOGETHER else "past one-at-a-time workers"
    if rate.mid is None:
        cost = f"{who}, {lane}: not enough data (no measured rows)"
    elif rate.basis == P.SEEDED:
        cost = f"{who}, {lane}: calibration/README.md figure, seeded, not fitted"
    else:
        low, high, basis = P.ratio_range(rate.samples)
        span = "P10-P90" if basis == P.FITTED else "min-max, few samples"
        cost = (f"{who}, {lane}: {rate.mid:.1f} five-hour points per 100k tokens, "
                f"{span} {low:.1f}-{high:.1f}, from {rate.count} {unit}")
    if short and rate.mid is not None and rate.basis != P.SEEDED:
        low, high, _ = P.ratio_range(rate.samples)
        cost = f"{rate.mid:.1f} pts/100k ({low:.1f}-{high:.1f}) from {rate.count} {unit}"
    medium = beliefs.tokens.get((provider, "medium"))
    if medium is None or medium.basis == P.SEEDED:
        tok = "tokens per row: seeded, not fitted"
    elif medium.basis == P.ALL_WORKERS and short:
        tok = f"tokens: median of {medium.count} workers"
    elif medium.basis == P.ALL_WORKERS:
        tok = (f"tokens per row: the median of {medium.count} finished workers "
               f"(~{medium.tokens / 1000:.0f}k), sizes not recorded yet")
    else:
        tok = "tokens per row: fitted from sized rows"
    return f"{cost} · {tok}"


def render_plan(candidates: list[TaskRow], picked: list[str], cursor: int, beliefs: Beliefs,
                provider: str, lane: str, width: int) -> dict:
    """Everything the page draws, as plain strings, so tests read the same words the screen shows:
    `{"head", "list": [...], "plan": [...], "totals": [...], "basis", "dispatch"}`."""
    phone = width <= PHONE_AT
    by_id = {r.id: r for r in candidates}
    chosen = [by_id[i] for i in picked if i in by_id]
    est = estimates(chosen, beliefs, provider, lane)
    who = PROVIDER_NAMES.get(provider, provider)

    head = f"PLAN · {who} · workers {lane}"
    if not phone:
        head += "   (space add/remove · t lane · c Claude/Codex · r re-read · q close)"

    order = {row_id: n + 1 for n, row_id in enumerate(picked)}
    listing = []
    for i, row in enumerate(candidates):
        mark = f"{order[row.id]:>2}" if row.id in order else " ·"
        pointer = ">" if i == cursor else " "
        tail = f"  {size_word(row):>7}"
        project = "" if phone else f"  {_shorten(row.project or '', 16):<16}"
        room = width - len(pointer) - len(mark) - len(tail) - len(project) - 4
        summary = _shorten(row.summary, max(8, room))
        listing.append(f"{pointer} {mark} {summary:<{max(8, room)}}{project}{tail}")
    if not candidates:
        listing.append("  no open rows in the queue")

    plan = []
    five_rows, five_total = est["five_hour"]
    week_rows, week_total = est["seven_day"]
    for n, row in enumerate(chosen):
        five, week = five_rows[n], week_rows[n]
        cost = f"5h {P.range_words(five)}" + ("" if phone else f"   wk {P.range_words(week)}")
        room = width - len(cost) - 8
        plan.append(f"{n + 1:>2}. {_shorten(row.summary, max(8, room)):<{max(8, room)}}  {cost}")

    now = beliefs.current.get(provider) or {}
    count = f"{len(chosen)} row{'s' if len(chosen) != 1 else ''}"
    if not chosen:
        totals = ["pick rows with space to cost a plan"]
    else:
        five_fit = P.fits(five_total, now.get("five_hour"))
        week_fit = P.fits(week_total, now.get("seven_day"))
        line = f"{count} · 5h: {P.range_words(five_total)}"
        if not phone:
            line += f" · wk: {P.range_words(week_total)}"
        totals = [line]
        if five_total.points is None:
            totals.append(f"{who}: not enough data to cost this plan")
        elif now.get("five_hour") is None:
            totals.append("no current reading of the 5-hour window, so no fit check")
        else:
            words = {"fits": "fits the 5-hour window"}.get(five_fit, f"5-hour window: {five_fit}")
            totals.append(f"{words} (now {P.pct_words(now.get('five_hour'))})"
                          + ("" if phone or week_fit == "?" else f" · week: {week_fit}"))
        labels = [l for l in five_total.labels if l != P.NO_DATA]
        if labels and not phone:
            totals.append("note: " + "; ".join(labels))

    return {
        "head": head,
        "list": listing,
        "plan": plan,
        "totals": totals,
        "basis": _basis_line(beliefs, provider, lane, short=phone),
        "dispatch": "[ dispatch this plan ]  not wired yet: dispatch rows from the queue",
        "total_five": five_total,
        "total_week": week_total,
        "rows_five": five_rows,
    }


# --------------------------------------------------------------------------- the window

class PlanApp(App):
    """One tmux window: the planning page."""

    CSS = """
    Screen { layout: vertical; background: $background; }
    #plan-head { height: 1; color: $primary; text-style: bold; }
    #plan-list { height: 1fr; border: round #1D5B6E; border-title-color: $primary; padding: 0 1; }
    #plan-plan { height: auto; max-height: 50%; border: round #1D5B6E; border-title-color: $primary;
                 padding: 0 1; }
    #plan-totals { height: auto; padding: 0 1; }
    #plan-basis { height: auto; padding: 0 1; color: $text-muted; }
    #plan-dispatch { height: auto; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("space", "toggle", "add/remove"),
        Binding("t", "lane", "lane"),
        Binding("c", "provider", "Claude/Codex"),
        Binding("r", "reload", "re-read"),
        Binding("q", "quit", "close"),
    ]

    def __init__(self, cfg=None, source=None,
                 limits_loader: Optional[Callable[[], list]] = None,
                 picture_loader: Optional[Callable[[], dict]] = None) -> None:
        super().__init__()
        self.cfg = cfg or config.load()
        self._source = source
        self._limits_loader = limits_loader or (lambda: limits.read_rows(self.cfg))
        self._picture_loader = picture_loader or (lambda: sources.read_hud(self.cfg))
        self.candidates: list[TaskRow] = []
        self.picked: list[str] = []
        self.cursor = 0
        self.lane = P.TOGETHER
        self.provider = "claude"
        self.beliefs = Beliefs.from_rows([])
        self.view: dict = {}

    def compose(self) -> ComposeResult:
        yield Static(id="plan-head")
        with Vertical(id="plan-list"):
            yield Static(id="plan-list-body")
        with Vertical(id="plan-plan"):
            yield Static(id="plan-plan-body")
        yield Static(id="plan-totals")
        yield Static(id="plan-basis")
        yield Static(id="plan-dispatch")

    def on_mount(self) -> None:
        try:
            theme.apply(self)
        except Exception:
            pass
        self.query_one("#plan-list").border_title = "QUEUE · open rows"
        self.query_one("#plan-plan").border_title = "THE PLAN · in the order added"
        self.action_reload()

    def on_resize(self, event) -> None:
        self.paint()

    # ---- data

    def action_reload(self) -> None:
        try:
            if self._source is None:
                from ..tasks import get_task_source
                self._source = get_task_source(self.cfg)
            else:
                self._source.refresh()
            self.candidates = open_rows(self._source.rows())
        except Exception:
            log.exception("could not read the task source")
            self.candidates = []
        try:
            rows = self._limits_loader() or []
        except Exception:
            log.exception("could not read the limits file")
            rows = []
        try:
            picture = self._picture_loader() or {}
        except Exception:
            picture = {}
        self.beliefs = Beliefs.from_rows(rows, picture)
        ids = {r.id for r in self.candidates}
        self.picked = [i for i in self.picked if i in ids]
        self.cursor = min(self.cursor, max(0, len(self.candidates) - 1))
        self.paint()

    # ---- keys

    def action_move(self, step: int) -> None:
        if self.candidates:
            self.cursor = max(0, min(len(self.candidates) - 1, self.cursor + int(step)))
            self.paint()

    def action_toggle(self) -> None:
        if not self.candidates:
            return
        row_id = self.candidates[self.cursor].id
        if row_id in self.picked:
            self.picked.remove(row_id)
        else:
            self.picked.append(row_id)
        self.paint()

    def action_lane(self) -> None:
        self.lane = P.SERIAL if self.lane == P.TOGETHER else P.TOGETHER
        self.paint()

    def action_provider(self) -> None:
        self.provider = "codex" if self.provider == "claude" else "claude"
        self.paint()

    # ---- drawing

    def paint(self) -> None:
        try:
            width = max(40, (self.size.width or 100) - 4)
        except Exception:
            width = 96
        self.view = render_plan(self.candidates, self.picked, self.cursor, self.beliefs,
                                self.provider, self.lane, width)
        v = self.view
        try:
            self.query_one("#plan-head", Static).update(Text(v["head"]))
            listing = Text()
            for i, line in enumerate(v["list"]):
                if i:
                    listing.append("\n")
                style = f"bold on {theme.TOKENS['cursor_band']}" if line.startswith(">") else ""
                listing.append(line, style=style)
            self.query_one("#plan-list-body", Static).update(listing)
            self.query_one("#plan-plan-body", Static).update(
                Text("\n".join(v["plan"]) or "nothing picked yet"))
            totals = Text()
            for i, line in enumerate(v["totals"]):
                if i:
                    totals.append("\n")
                warn = line.startswith("5-hour window: over") or "over by" in line
                tight = "tight" in line
                style = (f"bold {theme.TOKENS['error']}" if warn
                         else f"bold {theme.TOKENS['warning']}" if tight else "bold" if i == 0 else "")
                totals.append(line, style=style)
            self.query_one("#plan-totals", Static).update(totals)
            self.query_one("#plan-basis", Static).update(Text(v["basis"]))
            self.query_one("#plan-dispatch", Static).update(Text(v["dispatch"]))
        except Exception:
            # Not mounted yet (paint before compose) -- the next paint draws it.
            pass


def main() -> int:
    cfg = config.load()
    try:
        config.ensure_state_dirs(cfg)
        logging.basicConfig(filename=str(cfg.log_file), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    except OSError:
        pass
    PlanApp(cfg).run()
    return 0
