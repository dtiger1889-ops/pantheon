"""The stage against a fake tmux that models the pane moves it relies on:
`join-pane` empties (and so destroys) the source window, `break-pane -d` makes a new window at
the next free index, `list-panes`/`display-message` answer from the model. No real tmux."""
from __future__ import annotations

import json
import subprocess

from pantheon import config as config_mod
from pantheon import stage
from pantheon import tmuxctl
from pantheon.models import TmuxWindow


def cfg_for(tmp_path) -> config_mod.Config:
    return config_mod.Config(state_dir=str(tmp_path / "state"), tmux_session="pantheon")


class FakeTmux:
    def __init__(self, width: int = 200):
        self.width = width
        # index -> {"name": str, "panes": [pane ids]}
        self.windows = {0: {"name": "DECK", "panes": ["%0"]},
                        4: {"name": "loom-os", "panes": ["%4"]},
                        5: {"name": "habit_notes", "panes": ["%5"]}}
        self.calls: list[tuple] = []
        self.refuse: set[str] = set()

    def _window_of(self, target: str):
        if target.startswith("%"):
            for idx, w in self.windows.items():
                if target in w["panes"]:
                    return idx
            return None
        idx = int(target.split(":")[1])
        return idx if idx in self.windows else None

    def __call__(self, *args, tmux=None, check=False):
        self.calls.append(args)
        cmd = args[0]
        out, rc = "", 0
        if cmd in self.refuse:
            rc = 1
        elif cmd == "list-panes":
            idx = self._window_of(args[args.index("-t") + 1])
            if idx is None:
                rc = 1
            elif "-F" in args and args[args.index("-F") + 1].startswith("#{pane_active}"):
                w = self.windows[idx]
                last = len(w["panes"]) - 1
                out = "\n".join(f"{1 if i == last else 0}|{p}|{w['name']}" for i, p in enumerate(w["panes"]))
            elif "-F" in args and args[args.index("-F") + 1] == tmuxctl._FMT:
                w = self.windows[idx]
                out = "\n".join(f"{idx}|{w['name']}|node|C:/x/{p}|{p}|pantheon" for p in w["panes"])
            else:
                out = "\n".join(self.windows[idx]["panes"])
        elif cmd == "display-message":
            fmt = args[-1]
            idx = self._window_of(args[args.index("-t") + 1])
            if idx is None:
                rc = 1
            elif fmt == "#{window_width}":
                out = str(self.width)
            elif fmt == "#{window_name}":
                out = self.windows[idx]["name"]
            elif fmt == "#{pane_id}":
                out = self.windows[idx]["panes"][0]
        elif cmd == "join-pane":
            src = args[args.index("-s") + 1]
            dst = self._window_of(args[args.index("-t") + 1])
            src_idx = self._window_of(src)
            self.windows[src_idx]["panes"].remove(src)
            if not self.windows[src_idx]["panes"]:
                del self.windows[src_idx]
            self.windows[dst]["panes"].append(src)
        elif cmd == "break-pane":
            src = args[args.index("-s") + 1]
            name = args[args.index("-n") + 1]
            src_idx = self._window_of(src)
            if src_idx is None:
                rc = 1
            else:
                self.windows[src_idx]["panes"].remove(src)
                new = next(i for i in range(1, 100) if i not in self.windows)
                self.windows[new] = {"name": name, "panes": [src]}
                out = str(new)
        elif cmd == "list-sessions":
            out = "pantheon|pantheon"
        return subprocess.CompletedProcess(args, rc, out + ("\n" if out else ""), "")

    def named(self, cmd):
        return [c for c in self.calls if c[0] == cmd]


def _install(monkeypatch, fake):
    monkeypatch.setattr(tmuxctl, "run", fake)


def test_stage_joins_the_agent_beside_the_deck_and_gives_it_the_keyboard(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)

    result = stage.stage(cfg, 4, session_id="abc")

    assert result["ok"] and result["action"] == "staged"
    assert fake.windows[0]["panes"] == ["%0", "%4"]
    assert 4 not in fake.windows
    join = fake.named("join-pane")[0]
    assert "-h" in join and join[join.index("-t") + 1] == "%0"
    assert ("resize-pane", "-t", "%0", "-x", str(stage.SIDEBAR_COLUMNS)) in fake.calls
    assert fake.named("select-pane")[-1] == ("select-pane", "-t", "%4")
    saved = json.loads((tmp_path / "state" / "stage.json").read_text(encoding="utf-8"))
    assert saved == {"pane_id": "%4", "window_name": "loom-os", "session_id": "abc"}
    assert stage.current(cfg) == stage.Staged("%4", "loom-os", "abc")


def test_with_joins_off_a_click_only_switches_windows_and_moves_no_pane(tmp_path, monkeypatch):
    """The live default since 2026-09-27 (a join of a claude.exe pane wedged tmux): no join,
    no resize, no stage record -- just the session's own window."""
    fake = FakeTmux()
    _install(monkeypatch, fake)
    monkeypatch.setattr(stage, "JOIN_PANES", False)
    cfg = cfg_for(tmp_path)

    result = stage.stage(cfg, 4, session_id="abc")

    assert result["action"] == "switched"
    assert not fake.named("join-pane") and not fake.named("resize-pane")
    assert 4 in fake.windows
    assert stage.current(cfg) is None


def test_release_breaks_it_back_out_under_its_old_name_without_switching(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)

    result = stage.release(cfg)

    assert result["ok"] and result["action"] == "released"
    brk = fake.named("break-pane")[0]
    assert "-d" in brk and brk[brk.index("-n") + 1] == "loom-os"
    assert fake.windows[0]["panes"] == ["%0"]
    assert any(w["name"] == "loom-os" and w["panes"] == ["%4"] for w in fake.windows.values())
    assert stage.current(cfg) is None


def test_staging_a_second_session_puts_the_first_one_back(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)

    result = stage.stage(cfg, 5)

    assert result["action"] == "staged"
    assert fake.windows[0]["panes"] == ["%0", "%5"]
    assert any(w["name"] == "loom-os" and w["panes"] == ["%4"] for i, w in fake.windows.items() if i != 0)
    assert stage.current(cfg).pane_id == "%5"


def test_staging_the_staged_session_again_only_focuses_it(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)

    # The staged agent now reports as window 0 (it is the active pane there).
    result = stage.stage(cfg, 0)

    assert result["action"] == "focused"
    assert len(fake.named("join-pane")) == 1
    assert fake.named("select-pane")[-1] == ("select-pane", "-t", "%4")


def test_a_narrow_window_only_switches_to_the_agent_window(tmp_path, monkeypatch):
    fake = FakeTmux(width=80)
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)

    result = stage.stage(cfg, 4)

    assert result["action"] == "switched"
    assert fake.named("join-pane") == []
    assert ("select-window", "-t", "pantheon:4") in fake.calls
    assert stage.current(cfg) is None


def test_a_staged_agent_that_exited_is_cleared_on_the_next_check(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)
    fake.windows[0]["panes"].remove("%4")   # claude exited; tmux closed its pane

    assert stage.current(cfg) is not None           # no tmux question asked: record kept
    assert stage.current(cfg, verify=True) is None  # asked: it is gone, record cleared
    assert not (tmp_path / "state" / "stage.json").exists()


def test_unknown_is_never_treated_as_gone(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)
    fake.refuse.add("list-panes")          # tmux not answering

    assert stage.current(cfg, verify=True) is not None


def test_a_refused_join_changes_nothing_and_says_so(tmp_path, monkeypatch):
    fake = FakeTmux()
    fake.refuse.add("join-pane")
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)

    result = stage.stage(cfg, 4)

    assert not result["ok"] and "still in window 4" in result["message"]
    assert stage.current(cfg) is None


def test_release_with_nothing_staged_is_a_quiet_no_op(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    assert stage.release(cfg_for(tmp_path)) == {
        "ok": True, "action": "none", "message": "nothing was on screen beside the deck"}
    assert fake.calls == []


def test_release_that_tmux_refuses_is_reported_as_still_staged(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)
    fake.refuse.add("break-pane")

    result = stage.release(cfg)

    assert result["ok"] is False
    assert stage.current(cfg) is not None
    assert stage.main(["release"]) in (0, stage.EXIT_STILL_STAGED)   # CLI never raises


def test_cli_release_exit_codes(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(stage, "_cli_cfg", lambda: cfg)
    stage.stage(cfg, 4)
    fake.refuse.add("break-pane")
    assert stage.main(["release"]) == stage.EXIT_STILL_STAGED
    fake.refuse.clear()
    assert stage.main(["release"]) == 0
    assert stage.current(cfg) is None


def test_with_staged_adds_the_staged_pane_back_as_window_zero(tmp_path, monkeypatch):
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    base = [TmuxWindow(0, "DECK", "python", "C:/x", "%0", "pantheon")]
    assert stage.with_staged(base, cfg) is base          # nothing staged: untouched, no tmux call
    stage.stage(cfg, 4)

    windows = stage.with_staged(base, cfg)

    assert [w.pane_id for w in windows] == ["%0", "%4"]
    assert windows[1].index == 0 and windows[1].command == "node"


def test_a_window_that_is_gone_is_never_confused_with_the_current_pane(tmp_path, monkeypatch):
    # Throwaway-server trial 2026-09-26: display-message on a missing window falls back to the
    # current pane; list-panes fails instead, so nothing moves.
    fake = FakeTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)
    result = stage.stage(cfg, 4)            # window 4 went away when its pane was staged

    assert result == {"ok": False, "action": "none", "message": "window 4 is not there any more"}
    assert fake.windows[0]["panes"] == ["%0", "%4"]
    assert stage.current(cfg).pane_id == "%4"
