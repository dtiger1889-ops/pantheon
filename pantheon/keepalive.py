"""One error in a timer or a background worker must not take the whole deck down.

Textual ends the app on an exception from a `set_interval`/`set_timer` callback or from a
`run_worker` job (its default `exit_on_error=True`), prints the traceback to the pane and returns
from `run()`; the launcher's pane then closes on Enter and the traceback is gone (window 0 died
that way on 2026-10-07, cause never recovered). `KeepAlive` goes first in a widget's or app's
bases and wraps every callback those three methods are handed: an exception is logged to
`state/pantheon.log` with its traceback and the deck keeps drawing. Key and click handlers are
not covered; `deck/app.py` `main()` logs whatever still gets through.
"""
from __future__ import annotations

import functools
import inspect
import logging
import time

log = logging.getLogger("pantheon.keepalive")

# A callback failing on every 5-second tick would write a traceback every 5 seconds; the same
# error from the same place is written again only after this long.
REPEAT_LOG_SECONDS = 600.0

_last_logged: dict[tuple[str, str], float] = {}


def _report(where: str, exc: Exception) -> None:
    key = (where, type(exc).__name__)
    now = time.monotonic()
    last = _last_logged.get(key)
    if last is not None and now - last < REPEAT_LOG_SECONDS:
        return
    _last_logged[key] = now
    log.error("kept running after an error in %s", where, exc_info=exc)


def _name(fn) -> str:
    target = fn.func if isinstance(fn, functools.partial) else fn
    return getattr(target, "__qualname__", None) or repr(target)


def guarded(fn, where: str = ""):
    """`fn` wrapped so an exception is logged and swallowed. A coroutine function stays one, and a
    plain function that hands back an awaitable has it awaited inside the guard."""
    where = where or _name(fn)
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_guard(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                _report(where, exc)
                return None
        return async_guard

    @functools.wraps(fn)
    def guard(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            _report(where, exc)
            return None
        if inspect.isawaitable(result):
            return _awaited(result, where)
        return result
    return guard


async def _awaited(awaitable, where: str):
    try:
        return await awaitable
    except Exception as exc:
        _report(where, exc)
        return None


class KeepAlive:
    """Mixin for a Textual `App` or `Widget`: put it before the Textual base class."""

    def set_interval(self, interval, callback=None, **kwargs):
        return super().set_interval(interval, guarded(callback) if callback else callback, **kwargs)

    def set_timer(self, delay, callback=None, **kwargs):
        return super().set_timer(delay, guarded(callback) if callback else callback, **kwargs)

    def run_worker(self, work, *args, **kwargs):
        if callable(work):
            work = guarded(work)
        elif inspect.isawaitable(work):
            work = _awaited(work, repr(work))
        return super().run_worker(work, *args, **kwargs)
