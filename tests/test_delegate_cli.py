"""The CLI wiring (`pantheon.dispatch.delegate.main`, run as `pantheon-delegate` / eventually
`pantheon delegate`) -- argument parsing and exit codes, with the router itself faked out
(already covered end to end by test_delegate_router.py)."""
from __future__ import annotations

from pantheon.dispatch import delegate as delegate_mod


def test_cli_success_prints_message_and_exits_zero(monkeypatch, capsys):
    seen = {}

    def fake_delegate(task, **kwargs):
        seen["task"] = task
        seen["kwargs"] = kwargs
        return delegate_mod.DelegateResult(True, "tmux", "worker opened in window 4")

    monkeypatch.setattr(delegate_mod, "delegate", fake_delegate)
    code = delegate_mod.main(["do the thing", "--tier", "planner", "--dir", "/c/proj"])

    assert code == 0
    assert seen["task"] == "do the thing"
    assert seen["kwargs"]["tier"] == "planner"
    assert seen["kwargs"]["directory"] == "/c/proj"
    assert "worker opened in window 4" in capsys.readouterr().out


def test_cli_failure_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        delegate_mod, "delegate",
        lambda task, **kw: delegate_mod.DelegateResult(False, "fallback", "no task given"),
    )
    code = delegate_mod.main(["irrelevant"])
    assert code == 1
    assert "no task given" in capsys.readouterr().out


def test_cli_rejects_unknown_tier():
    import pytest

    with pytest.raises(SystemExit):
        delegate_mod.main(["task", "--tier", "not-a-real-tier"])
