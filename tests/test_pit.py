"""The pit, against a fake tmux that actually models
join-pane/break-pane semantics (a joined pane's source window is destroyed; the last pane left in
a window keeps that window) -- no real tmux server, no real subprocess.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess

from pantheon import config as config_mod
from pantheon import tmuxctl
from pantheon.models import TmuxWindow
from pantheon.pit import tmux_pit


def cfg_for(tmp_path) -> config_mod.Config:
    return config_mod.Config(state_dir=str(tmp_path / "state"), tmux_session="pantheon")


class FakeTmux:
    """Models exactly the tmux behaviour `tmux_pit` relies on: `join-pane` destroys the source
    window and moves its one pane into the target window; `break-pane` does the reverse; the
    window itself survives only as long as it has at least one pane -- so a `rename-window` on
    the pit's last remaining pane is how that pane's window gets its old name back (`leave_pit`'s
    last step), never a `break-pane` (there would be nothing left to move it away from)."""

    def __init__(self, windows: list[TmuxWindow], session: str = "pantheon"):
        self.calls: list[tuple] = []
        self.session = session
        # Regular one-pane windows: pane_id -> dict(index, name, path, command).
        self.regular: dict[str, dict] = {
            w.pane_id: {"index": w.index, "name": w.name, "path": w.path, "command": w.command}
            for w in windows
        }
        self.pit: dict | None = None   # {"index": int, "panes": [pane_id, ...]} once entered
        self._next_index = max((w.index for w in windows), default=0) + 1

    # -- helpers --------------------------------------------------------------
    def _index_of(self, target: str) -> int:
        return int(target.rsplit(":", 1)[-1])

    def _pane_at_index(self, index: int) -> str | None:
        for pane_id, w in self.regular.items():
            if w["index"] == index:
                return pane_id
        return None

    def _flag(self, args: tuple, name: str) -> str | None:
        return args[args.index(name) + 1] if name in args else None

    # -- the fake itself --------------------------------------------------------
    def run(self, *args: str, tmux=None, check: bool = False) -> subprocess.CompletedProcess:
        self.calls.append(args)
        cmd = args[0]

        if cmd == "list-windows":
            rows = [f"{w['index']}|{w['name']}|{w['command']}|{w['path']}|{pane_id}|{self.session}"
                    for pane_id, w in self.regular.items()]
            if self.pit is not None:
                first_pane = self.pit["panes"][0]
                rows.append(f"{self.pit['index']}|PIT|bash|C:/x|{first_pane}|{self.session}")
            out = "\n".join(rows) + ("\n" if rows else "")
            return subprocess.CompletedProcess(args, 0, out, "")

        if cmd == "display-message":
            fmt = args[-1]
            target = self._flag(args, "-t")
            if "pane_id" in fmt and target:
                pane_id = self._pane_at_index(self._index_of(target))
                return subprocess.CompletedProcess(args, 0, (pane_id or "") + "\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        if cmd == "rename-window":
            target = self._flag(args, "-t")
            new_name = args[-1]
            index = self._index_of(target)
            if self.pit is not None and self.pit["index"] == index:
                # The pit's last pane keeps this window, renamed back to what it was
                # (`leave_pit`'s final step -- there is only one pane left, nothing to break out).
                assert len(self.pit["panes"]) == 1, "renamed the pit window before it was empty"
                pane_id = self.pit["panes"][0]
                self.regular[pane_id] = {"index": index, "name": new_name, "path": "C:/x", "command": "bash"}
                self.pit = None
                return subprocess.CompletedProcess(args, 0, "", "")
            pane_id = self._pane_at_index(index)
            if pane_id is None:
                return subprocess.CompletedProcess(args, 1, "", "no such window")
            if new_name == tmux_pit.PIT_WINDOW:
                # This regular window becomes the pit itself (`enter_pit`'s first step).
                self.pit = {"index": index, "panes": [pane_id]}
                del self.regular[pane_id]
            else:
                self.regular[pane_id]["name"] = new_name
            return subprocess.CompletedProcess(args, 0, "", "")

        if cmd == "join-pane":
            src = self._flag(args, "-s")
            if self.pit is None or src not in self.regular:
                return subprocess.CompletedProcess(args, 1, "", "no such pane")
            del self.regular[src]
            self.pit["panes"].append(src)
            return subprocess.CompletedProcess(args, 0, "", "")

        if cmd == "select-layout":
            return subprocess.CompletedProcess(args, 0, "", "")

        if cmd == "list-panes":
            target = self._flag(args, "-t")
            if self.pit is not None and self.pit["index"] == self._index_of(target):
                out = "\n".join(self.pit["panes"]) + "\n"
                return subprocess.CompletedProcess(args, 0, out, "")
            return subprocess.CompletedProcess(args, 0, "", "")

        if cmd == "break-pane":
            src = self._flag(args, "-s")
            name = self._flag(args, "-n")
            if self.pit is None or src not in self.pit["panes"]:
                return subprocess.CompletedProcess(args, 1, "", "no such pane")
            self.pit["panes"].remove(src)
            new_index = self._next_index
            self._next_index += 1
            self.regular[src] = {"index": new_index, "name": name or src, "path": "C:/x", "command": "bash"}
            return subprocess.CompletedProcess(args, 0, f"{new_index}\n", "")

        return subprocess.CompletedProcess(args, 1, "", f"unhandled: {args}")


def three_agent_windows() -> list[TmuxWindow]:
    return [
        TmuxWindow(3, "loom-os", "claude", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon"),
        TmuxWindow(4, "hiking_log_v2", "claude", "C:/Home/x/Documents/Projects/hiking_log_v2", "%4", "pantheon"),
        TmuxWindow(5, "habit_notes", "codex", "C:/Home/x/Documents/Projects/habit_notes", "%5", "pantheon"),
    ]


# ---------------------------------------------------------------- agent_windows() filtering


def test_agent_windows_never_includes_the_decks_own_fixed_windows(monkeypatch):
    windows = three_agent_windows() + [
        TmuxWindow(0, "DECK", "python", "C:/x", "%0", "pantheon"),
        TmuxWindow(1, "QUEUE", "python", "C:/x", "%1", "pantheon"),
        TmuxWindow(2, "BUDGET", "python", "C:/x", "%2", "pantheon"),
        TmuxWindow(6, "NOTES", "python", "C:/x", "%6", "pantheon"),
    ]
    fake = FakeTmux(windows)
    monkeypatch.setattr(tmuxctl, "run", fake.run)

    names = {w.name for w in tmux_pit.agent_windows("pantheon")}
    assert names == {"loom-os", "hiking_log_v2", "habit_notes"}


# ---------------------------------------------------------------- acceptance 4


def test_enter_pit_joins_every_agent_window_into_one_tiled_window(tmp_path, monkeypatch):
    fake = FakeTmux(three_agent_windows())
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    cfg = cfg_for(tmp_path)

    result = tmux_pit.enter_pit(cfg)

    assert sorted(result["joined"]) == ["habit_notes", "hiking_log_v2", "loom-os"]
    assert result["pit_index"] is not None
    assert not result["already"]
    # Real windows: exactly one now (the pit) -- no window was left behind, none duplicated.
    windows_now = tmuxctl.list_windows("pantheon")
    assert [w.name for w in windows_now] == ["PIT"]
    assert fake.pit is not None and len(fake.pit["panes"]) == 3
    assert any(c[0] == "select-layout" and "tiled" in c for c in fake.calls)
    assert tmux_pit.is_active(cfg)


def test_leave_pit_restores_three_separate_windows_with_the_same_panes_alive(tmp_path, monkeypatch):
    fake = FakeTmux(three_agent_windows())
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    cfg = cfg_for(tmp_path)
    tmux_pit.enter_pit(cfg)

    restored = tmux_pit.leave_pit(cfg)

    assert sorted(restored) == ["habit_notes", "hiking_log_v2", "loom-os"]
    windows_now = {w.name: w.pane_id for w in tmuxctl.list_windows("pantheon")}
    assert windows_now == {"loom-os": "%3", "hiking_log_v2": "%4", "habit_notes": "%5"}
    assert fake.pit is None
    assert not tmux_pit.is_active(cfg)


def test_entering_the_pit_twice_is_a_no_op_the_second_time(tmp_path, monkeypatch):
    fake = FakeTmux(three_agent_windows())
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    cfg = cfg_for(tmp_path)
    tmux_pit.enter_pit(cfg)
    calls_before = len(fake.calls)

    result = tmux_pit.enter_pit(cfg)

    assert result["already"]
    assert result["joined"] == []
    assert len(fake.calls) == calls_before + 1   # only the window_index_by_name lookup


def test_leaving_when_not_in_the_pit_does_nothing(tmp_path, monkeypatch):
    fake = FakeTmux(three_agent_windows())
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    cfg = cfg_for(tmp_path)

    assert tmux_pit.leave_pit(cfg) == []
    assert fake.pit is None


def test_no_agent_windows_means_nothing_to_join(tmp_path, monkeypatch):
    fake = FakeTmux([TmuxWindow(0, "DECK", "python", "C:/x", "%0", "pantheon")])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    cfg = cfg_for(tmp_path)

    result = tmux_pit.enter_pit(cfg)

    assert result == {"joined": [], "pit_index": None, "already": False}
    assert not tmux_pit.is_active(cfg)


def test_pit_state_survives_across_separate_config_instances(tmp_path, monkeypatch):
    """`--pit` and `--unpit` are two separate `bin/pantheon` invocations (two Python processes),
    so the pane-name mapping has to live on disk, not in memory -- this is what proves it does."""
    fake = FakeTmux(three_agent_windows())
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    cfg1 = cfg_for(tmp_path)
    tmux_pit.enter_pit(cfg1)

    cfg2 = dataclasses.replace(cfg_for(tmp_path))  # a fresh Config, same state_dir
    state = json.loads((tmp_path / "state" / "pit.json").read_text(encoding="utf-8"))
    assert sorted(state.values()) == ["habit_notes", "hiking_log_v2", "loom-os"]

    restored = tmux_pit.leave_pit(cfg2)
    assert sorted(restored) == ["habit_notes", "hiking_log_v2", "loom-os"]
