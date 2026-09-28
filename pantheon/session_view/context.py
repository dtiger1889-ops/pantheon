"""The `ctx NN%` a session header shows.

Cause: the percentage divided the latest turn's tokens by a hard-coded 200k window for every
Claude model id without a `[1m]` marker. Every statusline capture on this box reports
`context_window.context_window_size = 1000000` -- for claude-opus-5-5, claude-opus-5,
claude-opus-4-8, claude-sonnet-5, claude-fable-5-1 and claude-opus-5[1m] alike -- so 624k tokens read as 312%.

Order now:
1. the session's own statusline capture (`bin/statusline_capture` writes
   `state/statusline/<session_id>.json`): its `context_window_size` is the denominator; with no
   usage in the transcript yet, its `used_percentage` is shown as is;
2. otherwise a model-aware window (`context_window_for`);
3. never over 100%: tokens past the assumed window mean the window is wrong, so the caption
   shows the token count instead (`ctx 624k`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

CONTEXT_WINDOW = 1_000_000        # every Claude model id seen on this box (see above)
CONTEXT_WINDOW_1M = 1_000_000
CONTEXT_WINDOW_CODEX = 258_400    # Codex rollouts report `model_context_window` ~258k


def context_window_for(model: Optional[str]) -> int:
    if not model:
        return CONTEXT_WINDOW
    low = model.lower()
    if "[1m]" in low:
        return CONTEXT_WINDOW_1M
    if "gpt" in low or "codex" in low or "astra" in low:
        return CONTEXT_WINDOW_CODEX
    return CONTEXT_WINDOW


def _num(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def statusline_window(statusline: Optional[dict]) -> tuple[Optional[int], Optional[float]]:
    """(context_window_size, used_percentage) from one statusline capture, either may be None."""
    block = (statusline or {}).get("context_window") or {}
    if not isinstance(block, dict):
        return None, None
    size = _num(block.get("context_window_size"))
    used = _num(block.get("used_percentage"))
    return (int(size) if size and size > 0 else None), used


def tokens_text(tokens: int) -> str:
    """`624k`, `1.2M`, `850` -- a context size in plain short form."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M".replace(".0M", "M")
    if tokens >= 1_000:
        return f"{int(round(tokens / 1_000))}k"
    return str(tokens)


def context_percent(tokens: int, model: Optional[str],
                    statusline: Optional[dict] = None) -> Optional[int]:
    """Whole percent of the window in use, or None when there is no number or it would pass 100
    (the window guess is wrong -- the caller shows tokens instead)."""
    window, used = statusline_window(statusline)
    if not tokens:
        if used is None:
            return None
        return max(0, min(100, int(round(used))))
    window = window or context_window_for(model)
    pct = 100.0 * tokens / window
    if pct > 100.0:
        return None
    return max(0, int(round(pct)))


def context_caption(tokens: int, model: Optional[str],
                    statusline: Optional[dict] = None) -> Optional[str]:
    """`ctx 62%`, or `ctx 624k` when the window is unknown / too small, or None with no data."""
    pct = context_percent(tokens, model, statusline)
    if pct is not None:
        return f"ctx {pct}%"
    if tokens:
        return f"ctx {tokens_text(int(tokens))}"
    return None


_default_dir: Optional[Path] = None


def statusline_for_session(session_id: Optional[str],
                           directory: Optional[Path] = None) -> dict:
    """This session's own statusline capture, `{}` when there is none. `directory` defaults to
    the configured `state/statusline`; any failure reads as `{}`."""
    global _default_dir
    if not session_id:
        return {}
    try:
        from ..hud import sources as sources_mod
        if directory is None:
            if _default_dir is None:
                from .. import config as config_mod
                _default_dir = config_mod.load().statusline_dir
            directory = _default_dir
        return sources_mod.statusline_for(directory, session_id) or {}
    except Exception:   # noqa: BLE001 -- a header caption must never take the deck down
        return {}
