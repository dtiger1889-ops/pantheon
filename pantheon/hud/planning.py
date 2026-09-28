"""Usage projections, pure arithmetic.

No file reads, no subprocess, no network, no Textual -- the same discipline as `calc.py`, so every
number the planning page or the BUDGET card shows can be unit-tested. The numbers it works from
come in as arguments: the per-token cost of each lane from `pantheon/planning/fit.py` (which reads
`state/limits/limits.jsonl` and re-uses `limits.refit`), the burn samples from `state/hud.json`.

Two questions, one answer shape each:

  - "where will this window land if the running work keeps going?" (`block_projection`): the
    ccusage `blocks` method -- current percent plus burn times the hours left before the reset --
    with a range from the percentages being whole numbers (`burn_band`);
  - "what will these queue rows cost?" (`row_estimate`, `plan_totals`, `fits`): expected tokens
    per row times the fitted points per 100k tokens, with a range from the spread of past rows
    (Claude-Code-Usage-Monitor's empirical percentile method: sort the past ratios, read P10 and
    P90; no normal curve assumed).

Every number carries how many past rows stand behind it, and says `not enough data` when there
are none. All of it is model arithmetic that the user has
not adopted as correct.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from . import calc

# Below this many past rows a percentile is not a percentile: the range widens to the full
# observed min..max and says so (spec step 1).
MIN_ROWS_FOR_INTERVAL = 5
P_LOW, P_HIGH = 10.0, 90.0

SIZES = ("small", "medium", "large")
# Seed tokens per queue-row size. Used only until sized rows exist on file; the screen then says `seeded`.
SEED_TOKENS = {"small": 60_000, "medium": 100_000, "large": 180_000}

# Lanes. The two are never averaged together: workers
# launched together cost several times what one-at-a-time workers do.
SERIAL, TOGETHER = "one at a time", "together"
LANES = (TOGETHER, SERIAL)
WINDOWS = ("five_hour", "seven_day")

# Basis words, printed on screen as they are.
FITTED = "fitted"
SEEDED = "seeded, not fitted"
FEW = "wide - few samples"
NO_DATA = "not enough data"
# Tokens for a size with no sized rows on file, while other finished workers exist: every size
# gets what the median worker took, spread P10..P90 across all of them. Truer than the seed
# table (the median worker on file took ~145k, above the 60k/100k seeds) until `size` is marked.
ALL_WORKERS = "sizes not recorded yet"

_TOKEN_LABELS = {SEEDED: "tokens seeded, not fitted", FEW: "tokens: few sized rows",
                 ALL_WORKERS: ALL_WORKERS}


# --------------------------------------------------------------------------- building blocks

def percentile(values: Iterable[float], q: float) -> Optional[float]:
    """The q-th percentile (0..100) by linear interpolation between closest ranks -- the same
    rule as numpy's default and Python's `statistics.quantiles(method="inclusive")`, so a hand
    check agrees. Empty -> None."""
    data = sorted(float(v) for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * max(0.0, min(100.0, float(q))) / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


@dataclass(frozen=True)
class Rate:
    """Points of one window per 100k tokens, for one provider and lane. `mid` is the pooled refit
    value (`limits.refit`); `samples` are the per-row (serial) or per-batch (together) ratios the
    range is read from."""

    mid: Optional[float]
    samples: tuple = ()
    basis: str = NO_DATA

    @property
    def count(self) -> int:
        return len(self.samples)


@dataclass(frozen=True)
class Tokens:
    """Expected tokens for one queue-row size, and the spread around it as factors of `tokens`
    (low, high) read from past rows, with how many rows that spread came from."""

    tokens: Optional[float]
    spread: tuple = (1.0, 1.0)
    count: int = 0
    basis: str = SEEDED


@dataclass(frozen=True)
class Estimate:
    """Expected points of one window, with a low..high range. `points is None` = not enough
    data. `rows` counts the past rows behind the per-token cost; `labels` are the plain words the
    screen prints beside the number (`seeded, not fitted`, `wide - few samples`, ...)."""

    tokens: Optional[float]
    points: Optional[float]
    low: Optional[float]
    high: Optional[float]
    rows: int = 0
    labels: tuple = field(default_factory=tuple)

    @property
    def known(self) -> bool:
        return self.points is not None


def ratio_range(samples: Sequence[float]) -> tuple[Optional[float], Optional[float], str]:
    """(low, high, basis) of the per-token ratios: P10/P90 with enough rows, the full min..max
    with fewer (`wide - few samples`), (None, None, not enough data) with none."""
    data = [float(s) for s in samples if s is not None]
    if not data:
        return None, None, NO_DATA
    if len(data) < MIN_ROWS_FOR_INTERVAL:
        return min(data), max(data), FEW
    return percentile(data, P_LOW), percentile(data, P_HIGH), FITTED


def _as_rate(value) -> Rate:
    if isinstance(value, Rate):
        return value
    if value is None:
        return Rate(None)
    return Rate(float(value), (), SEEDED)


def _as_tokens(value, size: str) -> Tokens:
    if isinstance(value, Tokens):
        return value
    if value is None:
        seed = SEED_TOKENS.get(size) or SEED_TOKENS["medium"]
        return Tokens(float(seed), (1.0, 1.0), 0, SEEDED)
    return Tokens(float(value), (1.0, 1.0), 0, FITTED)


def normal_size(size: Optional[str]) -> str:
    """A queue row's `est_context` as one of `SIZES`; anything else (unsized, `n/a`, typos) is
    `medium`, as the spec says."""
    text = (size or "").strip().lower()
    return text if text in SIZES else "medium"


# --------------------------------------------------------------------------- part (b): a plan

def row_estimate(size: Optional[str], provider: str, window: str,
                 token_table: Mapping, per_100k: Mapping) -> Estimate:
    """Expected points of `window` for one queue row, with a range.

    `token_table[(provider, size)]` is a `Tokens` or a plain number (a bare number counts as
    fitted with no spread); `per_100k[(provider, window)]` is a `Rate` or a plain number. The
    range is the P10..P90 of past ratios applied to this row's tokens, widened by the token
    spread when the token count itself is uncertain -- low tokens times low ratio, high tokens
    times high ratio, a best/worst band (see `plan_totals`). Never raises."""
    size = normal_size(size)
    tok = _as_tokens(token_table.get((provider, size)) if token_table else None, size)
    rate = _as_rate(per_100k.get((provider, window)) if per_100k else None)
    labels: list[str] = []
    if tok.basis != FITTED:
        labels.append(_TOKEN_LABELS.get(tok.basis, tok.basis))
    if rate.mid is None or tok.tokens is None:
        return Estimate(tok.tokens, None, None, None, rate.count, tuple(labels + [NO_DATA]))
    points = tok.tokens / 100_000.0 * rate.mid
    low_r, high_r, basis = ratio_range(rate.samples)
    if rate.basis == SEEDED:
        labels.append(f"cost {SEEDED}")
    if basis != FITTED:
        labels.append(basis if basis != NO_DATA else "no range: " + NO_DATA)
    if low_r is None or high_r is None:
        return Estimate(tok.tokens, points, None, None, rate.count, tuple(labels))
    t_low, t_high = tok.spread if tok.spread else (1.0, 1.0)
    low = tok.tokens * t_low / 100_000.0 * low_r
    high = tok.tokens * t_high / 100_000.0 * high_r
    return Estimate(tok.tokens, points, min(low, points), max(high, points), rate.count, tuple(labels))


def plan_totals(rows: Sequence[Estimate]) -> Estimate:
    """The whole plan: expected points summed, and the range as the sum of the per-row lows and
    the sum of the per-row highs. Deliberately NOT added in quadrature: the user reads the range as
    a best/worst band, not a variance. One row with
    no estimate makes the total unknown -- a partial sum would read as the whole plan."""
    rows = list(rows or ())
    if not rows:
        return Estimate(0.0, 0.0, 0.0, 0.0, 0, ())
    labels: list[str] = []
    for r in rows:
        for label in r.labels:
            if label not in labels:
                labels.append(label)
    tokens = sum(r.tokens or 0.0 for r in rows)
    past = min(r.rows for r in rows)
    if any(r.points is None for r in rows):
        return Estimate(tokens, None, None, None, past, tuple(labels))
    points = sum(r.points for r in rows)
    if any(r.low is None or r.high is None for r in rows):
        return Estimate(tokens, points, None, None, past, tuple(labels))
    return Estimate(tokens, points, sum(r.low for r in rows), sum(r.high for r in rows), past,
                    tuple(labels))


def fits(plan_total: Optional[Estimate], current_pct: Optional[float]) -> str:
    """`fits` when even the top of the range stays under 100% of the window, `tight (projected
    N%)` when the expected cost fits but the top of the range does not (N = the top), `over by
    N%` when the expected cost alone passes 100. `?` when either side is unknown."""
    if plan_total is None or plan_total.points is None or current_pct is None:
        return "?"
    try:
        current = float(current_pct)
    except (TypeError, ValueError):
        return "?"
    expected = current + plan_total.points
    top = current + (plan_total.high if plan_total.high is not None else plan_total.points)
    if expected > 100.0:
        return f"over by {expected - 100.0:.0f}%"
    if top > 100.0:
        return f"tight (projected {top:.0f}%)"
    return "fits"


# --------------------------------------------------------------------------- part (a): the card

def project_window(current_pct: Optional[float], burn_per_hour: Optional[float],
                   hours_ahead: Optional[float]) -> Optional[float]:
    """`current + burn x hours` -- ccusage's "if the current rate continues". Not clamped here
    (the screen clamps); None when any input is None or unreadable."""
    try:
        if current_pct is None or burn_per_hour is None or hours_ahead is None:
            return None
        return float(current_pct) + float(burn_per_hour) * max(0.0, float(hours_ahead))
    except (TypeError, ValueError):
        return None


def _clean(samples) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for item in samples or ():
        if not item or len(item) < 2:
            continue
        t = calc._as_epoch(item[0])
        if t is None or item[1] is None:
            continue
        try:
            out.append((t, float(item[1])))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p[0])
    return out


def burn_band(samples, now: Optional[float] = None,
              window_seconds: int = calc.BURN_WINDOW_SECONDS) -> Optional[dict]:
    """The burn rate with the range its readings allow: `{mid, low, high, readings, hours}` in
    percent per hour. `mid` is `calc.burn_pct_per_hour` itself; the range comes from the plan
    percentages being whole numbers, so a rise of d points between two readings is really
    anything from d-1 to d+1. None whenever `calc` says None --
    no projection is ever drawn from a burn the card does not show."""
    mid = calc.burn_pct_per_hour(samples, window_seconds=window_seconds, now=now)
    if mid is None:
        return None
    clean = _clean(samples)
    # The same tail calc measured over: the rising run since the last reset, inside the window.
    tail = [clean[-1]]
    for t, pct in reversed(clean[:-1]):
        if pct > tail[0][1]:
            break
        tail.insert(0, (t, pct))
    newest = tail[-1][0]
    tail = [(t, p) for t, p in tail if newest - t <= window_seconds]
    hours = (tail[-1][0] - tail[0][0]) / 3600.0
    if len(tail) < 2 or hours <= 0:
        return None
    delta = tail[-1][1] - tail[0][1]
    return {
        "mid": mid,
        "low": max(0.0, delta - 1.0) / hours,
        "high": (delta + 1.0) / hours,
        "readings": len(tail),
        "hours": hours,
    }


def block_projection(current_pct: Optional[float], samples, resets_at,
                     now: Optional[float] = None) -> Optional[dict]:
    """Where the 5-hour window lands at its reset if the current burn continues:
    `{pct, low, high, by, readings, caution}`. `by` is the reset time (ISO, as given). `caution`
    is True when the top of the range crosses 90% before the reset.
    None when there is no current percent, no burn, or no future reset -- silence over a guess.

    The running-builds variant in the spec (hours left of each live agent's expected tokens) is
    not drawn: live agents carry no size and no token count yet, so it would be a guess."""
    if current_pct is None:
        return None
    band = burn_band(samples, now=now)
    if band is None:
        return None
    left = calc.seconds_until(resets_at, now)
    if left is None:
        return None
    hours = left / 3600.0
    pct = project_window(current_pct, band["mid"], hours)
    low = project_window(current_pct, band["low"], hours)
    high = project_window(current_pct, band["high"], hours)
    if pct is None or low is None or high is None:
        return None
    return {
        "pct": pct, "low": low, "high": high, "by": resets_at,
        "readings": band["readings"], "caution": high >= 90.0,
    }


# --------------------------------------------------------------------------- words

def pct_words(value: Optional[float]) -> str:
    """`34%`, `<1%`, `100%` (anything past 100 is shown as 100: the bar stops there), `?`."""
    if value is None:
        return "?"
    v = float(value)
    if v >= 100.0:
        return "100%"
    if 0 < v < 0.5:
        return "<1%"
    return f"{max(0.0, v):.0f}%"


def _bare(value: float) -> str:
    return pct_words(value).rstrip("%")


def range_words(est: Optional[Estimate]) -> str:
    """`34% (28-46%)`, `34% (no range)`, or `not enough data`."""
    if est is None or est.points is None:
        return NO_DATA
    if est.low is None or est.high is None:
        return f"{pct_words(est.points)} (no range)"
    return f"{pct_words(est.points)} ({_bare(est.low)}-{pct_words(est.high)})"


def projection_words(proj: Optional[dict], clock_text: str = "") -> Optional[str]:
    """`70% (64-78%) by 01:10 · 3 readings` for the BUDGET card; None when there is none."""
    if not proj:
        return None
    by = f" by {clock_text}" if clock_text else ""
    n = proj.get("readings") or 0
    return (f"{pct_words(proj['pct'])} ({_bare(proj['low'])}-{pct_words(proj['high'])}){by}"
            f" · {n} reading{'s' if n != 1 else ''}")

