"""One error in a timer or a worker must not end the deck (window 0 died that way, 2026-10-07)."""
import asyncio
import logging

from textual.app import App
from textual.widgets import Static

from pantheon import keepalive


def _fresh(monkeypatch):
    monkeypatch.setattr(keepalive, "_last_logged", {})


def test_guarded_swallows_and_logs_once_per_window(monkeypatch, caplog):
    _fresh(monkeypatch)
    calls = []

    def boom():
        calls.append(1)
        raise ValueError("bad row")

    wrapped = keepalive.guarded(boom, "the tick")
    with caplog.at_level(logging.ERROR, logger="pantheon.keepalive"):
        assert wrapped() is None
        assert wrapped() is None
    assert len(calls) == 2
    records = [r for r in caplog.records if "kept running" in r.getMessage()]
    assert len(records) == 1                       # the second, identical error is not written again
    assert "the tick" in records[0].getMessage()
    assert records[0].exc_info is not None         # the traceback goes to the log


def test_guarded_passes_results_through(monkeypatch):
    _fresh(monkeypatch)
    assert keepalive.guarded(lambda: 7)() == 7

    async def coro():
        return 8
    assert asyncio.run(keepalive.guarded(coro)()) == 8


def test_guarded_async_failure_is_swallowed(monkeypatch, caplog):
    _fresh(monkeypatch)

    async def boom():
        raise RuntimeError("async tick")

    with caplog.at_level(logging.ERROR, logger="pantheon.keepalive"):
        assert asyncio.run(keepalive.guarded(boom)()) is None
    assert any("kept running" in r.getMessage() for r in caplog.records)


class _Deckish(keepalive.KeepAlive, App):
    def compose(self):
        yield Static("ok")

    def on_mount(self):
        self.ticks = 0
        self.worked = False
        self.set_interval(0.01, self._tick)
        self.run_worker(self._bad_worker, thread=True)
        self.run_worker(self._good_worker, thread=True)

    def _tick(self):
        self.ticks += 1
        raise BlockingIOError(11, "Resource temporarily unavailable")

    def _bad_worker(self):
        raise OSError(12, "Cannot allocate memory")

    def _good_worker(self):
        self.worked = True


def test_app_survives_a_failing_timer_and_worker(monkeypatch):
    _fresh(monkeypatch)

    async def drive():
        app = _Deckish()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert app.is_running
            assert app.ticks >= 2                  # the timer kept firing after its first error
            assert app.worked
        return app

    app = asyncio.run(drive())
    assert app._exception is None
    assert (app.return_code or 0) == 0
