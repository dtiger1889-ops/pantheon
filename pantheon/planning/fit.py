"""What the planner believes, re-fitted from `state/limits/limits.jsonl` on every read
.

The only file in the planner that reads the limits file. It adds no arithmetic of its own for the
per-token cost: `limits.refit`
gives the pooled points per 100k tokens for each lane, and its `detail` lists give the per-row and
per-batch ratios the range is read from. Tokens per queue-row size come from `after` marks that
carry a `size` (`usage_mark after --size medium`); a size with none falls back to the seed table
in `hud/planning.py` and says `seeded, not fitted` on screen.

`python -m pantheon.planning.fit` (`bin/plan_show`) prints all of it as JSON.
"""
from __future__ import annotations

import json
import sys
from typing import Iterable, Optional

from ..hud import limits
from ..hud import planning as P

PROVIDERS = ("claude", "codex")

# The README's measured figures, used ONLY when `refit` finds nothing on file for that lane -- e.g. a fresh
# state directory. Labelled `seeded, not fitted` on screen. Codex has none: it is not measured.
README_FALLBACK = {
    ("claude", P.SERIAL, "five_hour"): 1.5,
    ("claude", P.SERIAL, "seven_day"): 0.16,
    ("claude", P.TOGETHER, "five_hour"): 3.1,
    ("claude", P.TOGETHER, "seven_day"): 0.40,
}

_REFIT_KEYS = {
    (P.SERIAL, "five_hour"): ("five_hour_points_per_100k", "serial_five_hour_ratios"),
    (P.SERIAL, "seven_day"): ("seven_day_points_per_100k", "serial_seven_day_ratios"),
    (P.TOGETHER, "five_hour"): ("batch_five_hour_points_per_100k", "batch_five_hour_ratios"),
    (P.TOGETHER, "seven_day"): ("batch_seven_day_points_per_100k", "batch_seven_day_ratios"),
}


def _tokens_of(row: dict) -> int:
    try:
        return int(float(row.get("tokens") or 0))
    except (TypeError, ValueError):
        return 0


def _finished_workers(rows: Iterable[dict]) -> list[dict]:
    return [r for r in rows or () if r.get("event") == "after" and _tokens_of(r) > 0]


def _spread(values: list[float], centre: float) -> tuple[float, float]:
    """(low, high) of `values` as factors of `centre`: P10/P90 with enough rows, min/max with
    fewer, (1, 1) with none."""
    if not values or not centre:
        return 1.0, 1.0
    low, high, _ = P.ratio_range(values)
    return (low or centre) / centre, (high or centre) / centre


def token_table(rows: Iterable[dict]) -> dict:
    """`(provider, size) -> planning.Tokens`. A size with sized `after` rows on file: their mean,
    spread P10..P90 (min..max under five rows). A size with none, while other finished workers
    exist: the median worker's tokens, spread P10..P90 across every worker (`sizes not recorded
    yet` -- the honest answer to "we do not know how big this row is"). Nothing on file at all:
    the spec's seed table, `seeded, not fitted`."""
    rows = list(rows or ())
    workers = _finished_workers(rows)
    out: dict = {}
    for provider in PROVIDERS:
        mine = [r for r in workers if limits._provider(r) == provider]
        every = [float(_tokens_of(r)) for r in mine]
        median = P.percentile(every, 50.0)
        for size in P.SIZES:
            sized = [float(_tokens_of(r)) for r in mine if str(r.get("size") or "").lower() == size]
            if sized:
                mean = sum(sized) / len(sized)
                spread = _spread(sized, mean)
                basis = P.FITTED
                if len(sized) < P.MIN_ROWS_FOR_INTERVAL:
                    # A handful of sized rows says little about their spread (one row says
                    # nothing): keep at least the width every worker on file shows.
                    basis = P.FEW
                    wide = _spread(every, median) if median else (1.0, 1.0)
                    spread = (min(spread[0], wide[0]), max(spread[1], wide[1]))
                out[(provider, size)] = P.Tokens(mean, spread, len(sized), basis)
            elif median:
                out[(provider, size)] = P.Tokens(median, _spread(every, median), len(every),
                                                 P.ALL_WORKERS)
            else:
                out[(provider, size)] = P.Tokens(float(P.SEED_TOKENS[size]), (1.0, 1.0), 0, P.SEEDED)
    return out


def per_100k(rows: Iterable[dict], lane: str = P.TOGETHER) -> dict:
    """`(provider, window) -> planning.Rate` for one lane (`planning.TOGETHER` or `SERIAL`).
    Fitted from `limits.refit`; the README figure (labelled seeded) only when refit has nothing;
    `Rate(None)` -- not enough data -- when neither exists (Codex today)."""
    rows = list(rows or ())
    out: dict = {}
    for provider in PROVIDERS:
        fit = limits.refit(rows, provider, detail=True)
        for window in P.WINDOWS:
            mid_key, list_key = _REFIT_KEYS[(lane, window)]
            mid = fit.get(mid_key)
            samples = tuple(v for v in (fit.get(list_key) or ()) if v is not None)
            if mid is not None:
                out[(provider, window)] = P.Rate(float(mid), samples, P.FITTED)
            elif (provider, lane, window) in README_FALLBACK:
                out[(provider, window)] = P.Rate(README_FALLBACK[(provider, lane, window)], (), P.SEEDED)
            else:
                out[(provider, window)] = P.Rate(None, (), P.NO_DATA)
    return out


def believes(rows: Iterable[dict]) -> dict:
    """Everything the planner is working from, as plain JSON: the token table and both lanes'
    per-100k figures with their ranges and row counts, and one worked estimate per size."""
    rows = list(rows or ())
    table = token_table(rows)
    lanes = {lane: per_100k(rows, lane) for lane in P.LANES}
    out: dict = {"tokens": {}, "per_100k": {}, "row_estimates": {}}
    for (provider, size), t in table.items():
        out["tokens"][f"{provider} {size}"] = {
            "tokens": round(t.tokens) if t.tokens is not None else None,
            "low_factor": round(t.spread[0], 3), "high_factor": round(t.spread[1], 3),
            "rows": t.count, "basis": t.basis,
        }
    for lane, rates in lanes.items():
        for (provider, window), rate in rates.items():
            low, high, basis = P.ratio_range(rate.samples)
            out["per_100k"][f"{provider} {lane} {window}"] = {
                "mid": _r(rate.mid), "low": _r(low), "high": _r(high), "rows": rate.count,
                "basis": rate.basis if rate.mid is None or rate.basis != P.FITTED else basis,
            }
            for size in P.SIZES:
                est = P.row_estimate(size, provider, window, table, rates)
                out["row_estimates"][f"{provider} {lane} {window} {size}"] = {
                    "points": _r(est.points), "low": _r(est.low), "high": _r(est.high),
                    "rows": est.rows, "labels": list(est.labels),
                    "words": P.range_words(est),
                }
    return out


def _r(value: Optional[float], places: int = 2) -> Optional[float]:
    return None if value is None else round(float(value), places)


def main(argv: Optional[list[str]] = None) -> int:
    from .. import config as config_mod

    cfg = config_mod.load()
    print(json.dumps(believes(limits.read_rows(cfg)), indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
