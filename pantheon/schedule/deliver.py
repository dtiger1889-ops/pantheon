"""The fire-time half of "prompt an EXISTING session at a set time" (`bin/schedule --session`).

Task Scheduler (see `schedule.py`) runs `bin/schedule --fire <id>` at the chosen minute; for a
prompt note that lands here. The prompt goes in through `assistant_telegram.paste_when_idle`, the
same safe delivery the Assistant's Telegram link uses: nothing is typed unless a fresh screen read
shows Claude Code idle at an empty prompt. If it is not (a turn is running, a question or dialog
is up, text is half-typed) we look again every `RETRY_SECONDS`, up to `MAX_TRIES` looks, then give
up, write down why, and notify through the deck's existing channels. There is no second scheduler:
this loop only runs inside the one fire Task Scheduler started.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .. import assistant_telegram as tg
from .. import session_ctl
from .. import tmuxctl

RETRY_SECONDS = 60
MAX_TRIES = 30                     # 30 looks, a minute apart: half an hour of waiting for an idle prompt
PASTE_BUFFER = "pantheon-schedule"

Opener = Callable[[list], tuple]


def find_windows(cfg, name: str, tmux: Optional[str] = None) -> list:
    """Windows in the deck's tmux session whose name is `name` (case-insensitive, as the deck
    shows it). More than one means the name is ambiguous."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return []
    tmux = tmux if tmux is not None else cfg.tools.tmux
    return [w for w in tmuxctl.list_windows(cfg.tmux_session, tmux=tmux) if w.name.lower() == wanted]


def check_name_now(cfg, name: str, tmux: Optional[str] = None) -> Optional[str]:
    """For `bin/schedule` at creation time: a sentence when tmux answers and `name` is not one
    window of it, else `None`. tmux silent (the deck is on the other Windows session, or closed)
    means it cannot be checked, which is not an error."""
    tmux = tmux if tmux is not None else cfg.tools.tmux
    every = tmuxctl.list_windows(cfg.tmux_session, tmux=tmux)
    if not every:
        return None
    hits = [w for w in every if w.name.lower() == (name or "").strip().lower()]
    if len(hits) == 1:
        return None
    names = ", ".join(sorted({w.name for w in every}))
    if not hits:
        return f"no session window is named '{name}' right now. Windows: {names}."
    return f"{len(hits)} session windows are named '{name}'; rename one so the prompt knows where to go."


def pane_now(cfg, name: str, tmux: Optional[str] = None) -> Optional[str]:
    """The tmux pane id of the one window named `name`, recorded when the prompt is scheduled so
    the fire types only into that same conversation, never a later one that took the name.
    `None` when tmux is silent or the name is not one window."""
    hits = find_windows(cfg, name, tmux=tmux)
    return hits[0].pane_id if len(hits) == 1 and hits[0].pane_id else None


def notify(cfg, title: str, body: str, send_toast=None, send_ntfy=None, send_telegram=None) -> list:
    """Tell the user through whichever notify channels they have switched on (`[notify]`), the same
    three the notifier uses. Returns `[(channel, ok, detail)]`; never raises."""
    from ..notify import channels

    settings = cfg.notify_settings()
    send_toast = send_toast or channels.toast
    send_ntfy = send_ntfy or channels.ntfy
    send_telegram = send_telegram or channels.telegram
    out = []
    try:
        if settings.toast_enabled:
            out.append(("toast", *send_toast(title, body, cfg.tools.pwsh)))
        if settings.ntfy_enabled:
            out.append(("ntfy", *send_ntfy(settings.ntfy_url, settings.ntfy_topic, title, body, "high")))
        if settings.telegram_enabled:
            out.append(("telegram", *send_telegram(f"{title}\n{body}", settings.telegram_chat_id,
                                                   settings.telegram_credential_target, cfg.tools.pwsh)))
    except Exception as exc:    # a notice failing must never hide the result it was about
        out.append(("error", False, f"{type(exc).__name__}: {exc}"))
    return out


def run(cfg, manifest: dict, opener: Optional[Opener] = None, tmux: Optional[str] = None,
        capture: Optional[Callable[..., str]] = None, sleep: Callable[[float], None] = time.sleep,
        retry_seconds: float = RETRY_SECONDS, max_tries: int = MAX_TRIES,
        notifier: Optional[Callable[[str, str], object]] = None,
        progress: Optional[Callable[[int], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None) -> tuple:
    """Deliver `manifest["prompt"]` into the window named `manifest["target"]`. Returns
    `(code, said)`: 0 and a sentence when it was typed in; 1 and the reason when it was not (the
    reason is also sent to `notifier`, default `notify`). `opener` starts a new session from
    `manifest["open_args"]` when the window is gone and `manifest["or_new"]` is set. `cancelled`
    is asked before every look; true means `bin/schedule --cancel` ran while we waited, so nothing
    is typed and nobody is notified."""
    capture = capture or tmuxctl.capture
    tmux = tmux if tmux is not None else cfg.tools.tmux
    notifier = notifier or (lambda title, body: notify(cfg, title, body))
    target = manifest.get("target") or ""
    prompt = manifest.get("prompt") or ""

    def fail(said: str) -> tuple:
        notifier(f"Scheduled prompt for {target} was not sent", said)
        return 1, said

    if not prompt.strip():
        return fail("the note holds no prompt text; nothing was typed")
    for attempt in range(1, max(1, max_tries) + 1):
        if cancelled is not None and cancelled():
            return 1, "cancelled before it was sent; nothing was typed"
        if progress:
            progress(attempt)
        hits = find_windows(cfg, target, tmux=tmux)
        if manifest.get("pane_id"):
            # Only the conversation that had the name when this was scheduled; a newer session
            # under the same name never gets text written for the old one.
            hits = [w for w in hits if w.pane_id == manifest["pane_id"]]
        if len(hits) > 1:
            return fail(f"{len(hits)} session windows are named '{target}', so I did not guess; nothing was typed")
        if not hits:
            if manifest.get("or_new") and opener is not None and manifest.get("open_args"):
                code, said = opener(list(manifest["open_args"]))
                if code == 0:
                    return 0, f"'{target}' was gone, so a new session was started: {said}"
                return fail(f"'{target}' was gone and the new session did not start: {said}")
            return fail(f"the session '{target}' is gone, so the prompt was not sent "
                        f"(add --or-new when scheduling to start a new one instead)")
        window = hits[0]
        pane = window.pane_id or f"{window.session}:{window.index}"
        if not session_ctl.is_claude_window(pane, tmux=tmux):
            return fail(f"'{target}' is not running Claude Code right now, so nothing was typed")
        state = tg.paste_when_idle(pane, prompt, tmux=tmux, capture=capture, sleep=sleep, buffer=PASTE_BUFFER)
        if state == "sent":
            return 0, f"prompt typed into '{target}'" + (f" (look {attempt})" if attempt > 1 else "")
        if attempt < max_tries:
            sleep(retry_seconds)
    return fail(f"'{target}' stayed busy or had a question on screen for {max_tries} looks "
                f"{int(retry_seconds)} seconds apart, so the prompt was not sent")
