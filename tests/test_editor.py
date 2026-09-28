"""The editor pane against the stage's fake tmux, grown the few commands the
editor adds: `split-window` / `new-window` make a new pane, `list-panes -s` answers for the whole
session, and a pane can be "quit" (removed) the way tmux closes it when the editor exits."""
from __future__ import annotations

import subprocess

from pantheon import config as config_mod
from pantheon import editor
from pantheon import stage
from tests.test_stage import FakeTmux, _install


def cfg_for(tmp_path, command: str = "") -> config_mod.Config:
    return config_mod.Config(state_dir=str(tmp_path / "state"), tmux_session="pantheon",
                             editor={"command": command} if command else {})


def only_nano(name):
    return "/usr/bin/nano" if name == "nano" else None


class EditorTmux(FakeTmux):
    def __init__(self, width: int = 200):
        super().__init__(width)
        self.next_pane = 50
        self.shells: list[str] = []

    def quit_editor(self, pane: str) -> None:
        for idx, w in list(self.windows.items()):
            if pane in w["panes"]:
                w["panes"].remove(pane)
                if not w["panes"]:
                    del self.windows[idx]

    def __call__(self, *args, tmux=None, check=False):
        cmd = args[0]
        if cmd in self.refuse:
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "")
        if cmd == "list-panes" and "-s" in args:
            self.calls.append(args)
            out = "\n".join(f"{p}|{i}" for i, w in self.windows.items() for p in w["panes"])
            return subprocess.CompletedProcess(args, 0, out + "\n", "")
        if cmd == "split-window":
            self.calls.append(args)
            target = args[args.index("-t") + 1]
            idx = self._window_of(target)
            pane = f"%{self.next_pane}"
            self.next_pane += 1
            self.windows[idx]["panes"].append(pane)
            self.shells.append(args[-1])
            return subprocess.CompletedProcess(args, 0, pane + "\n", "")
        if cmd == "new-window":
            self.calls.append(args)
            pane = f"%{self.next_pane}"
            self.next_pane += 1
            new = next(i for i in range(1, 100) if i not in self.windows)
            self.windows[new] = {"name": args[args.index("-n") + 1], "panes": [pane]}
            self.shells.append(args[-1])
            return subprocess.CompletedProcess(args, 0, pane + "\n", "")
        if cmd in ("resize-pane", "select-pane", "select-window", "kill-pane"):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return super().__call__(*args, tmux=tmux, check=check)


# ------------------------------------------------------------------ which editor


def test_default_is_micro_when_installed_else_nano_else_editor_env(tmp_path):
    cfg = cfg_for(tmp_path)
    both = lambda n: f"/usr/bin/{n}" if n in ("micro", "nano") else None
    assert editor.resolve(cfg, which=both, env={}).name == "micro"
    assert editor.resolve(cfg, which=only_nano, env={}).name == "nano"
    vim = lambda n: "/usr/bin/vim" if n == "vim" else None
    assert editor.resolve(cfg, which=vim, env={"EDITOR": "vim"}).argv == ["/usr/bin/vim"]


def test_no_editor_at_all_is_said_in_plain_words(tmp_path):
    found = editor.resolve(cfg_for(tmp_path), which=lambda n: None, env={})
    assert not found.ok
    assert found.problem == editor.NOT_FOUND
    assert "no text editor found" in found.problem and "[editor] command" in found.problem


def test_a_window_program_in_editor_env_is_passed_over(tmp_path):
    found = editor.resolve(cfg_for(tmp_path), which=lambda n: f"/x/{n}", env={"EDITOR": "notepad"})
    # micro is "installed" here too, so to see the $EDITOR rule, hide micro and nano.
    hidden = lambda n: None if n in ("micro", "nano") else f"/x/{n}"
    assert found.name == "micro"
    assert not editor.resolve(cfg_for(tmp_path), which=hidden, env={"EDITOR": "notepad"}).ok


def test_the_toml_setting_wins_and_is_never_quietly_swapped(tmp_path):
    nvim = lambda n: "/usr/bin/nvim" if n == "nvim" else "/usr/bin/nano" if n == "nano" else None
    found = editor.resolve(cfg_for(tmp_path, "nvim"), which=nvim, env={})
    assert found.argv == ["/usr/bin/nvim"] and found.name == "nvim"
    missing = editor.resolve(cfg_for(tmp_path, "hx"), which=nvim, env={})
    assert not missing.ok and '"hx"' in missing.problem and "not installed" in missing.problem
    flags = editor.resolve(cfg_for(tmp_path, "nano -l"), which=nvim, env={})
    assert flags.argv == ["/usr/bin/nano", "-l"]


def test_line_numbers_only_for_editors_that_take_them():
    nano = editor.Editor(["/usr/bin/nano"], "nano")
    assert editor.command_line(nano, "C:\\x\\a b.md", 12) == "/usr/bin/nano +12 'C:/x/a b.md'"
    hx = editor.Editor(["/usr/bin/hx"], "hx")
    assert editor.command_line(hx, "/x/a.md", 3) == "/usr/bin/hx /x/a.md:3"
    other = editor.Editor(["/usr/bin/ed"], "ed")
    assert editor.command_line(other, "/x/a.md", 3) == "/usr/bin/ed /x/a.md"


def test_typed_paths_are_relative_to_the_selected_folder():
    assert editor.resolve_path("notes/a.md", "C:/w/loom-os") == "C:/w/loom-os/notes/a.md"
    assert editor.resolve_path('"C:\\w\\x.md"', "C:/w/loom-os") == "C:/w/x.md"
    assert editor.resolve_path("/etc/hosts", "C:/w") == "/etc/hosts"
    assert editor.resolve_path("   ", "C:/w") == ""


# ------------------------------------------------------------------ opening beside the list


def test_e_opens_the_editor_beside_the_staged_session_both_visible(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    assert stage.stage(cfg, 4)["action"] == "staged"
    result = editor.open_beside_stage(cfg, "C:/w/loom-os/CHECKPOINT.md", which=only_nano, env={})
    assert result["ok"] and result["action"] == "opened"
    assert "CHECKPOINT.md is open in nano beside the list" in result["message"]
    pane = result["pane_id"]
    assert fake.windows[0]["panes"] == ["%0", "%4", pane]          # sidebar | session | editor
    split = fake.named("split-window")[0]
    assert split[split.index("-t") + 1] == "%4" and "-h" in split
    assert fake.shells[-1] == "/usr/bin/nano C:/w/loom-os/CHECKPOINT.md"
    assert ("resize-pane", "-t", "%0", "-x", str(stage.SIDEBAR_COLUMNS)) in fake.calls
    assert fake.named("select-pane")[-1] == ("select-pane", "-t", pane)
    assert editor.is_beside(cfg)


def test_with_nothing_staged_the_editor_goes_beside_the_sidebar(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    result = editor.open_beside_stage(cfg, "/x/a.md", cwd="/x", which=only_nano, env={})
    assert result["action"] == "opened"
    split = fake.named("split-window")[0]
    assert split[split.index("-t") + 1] == "%0" and split[split.index("-c") + 1] == "/x"


def test_quitting_the_editor_removes_only_its_pane_and_the_record(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)
    pane = editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})["pane_id"]
    fake.quit_editor(pane)
    assert editor.where(cfg) is None
    assert fake.windows[0]["panes"] == ["%0", "%4"]     # the stage untouched
    assert stage.current(cfg, verify=True).pane_id == "%4"
    assert not fake.named("kill-pane")


def test_a_second_e_never_opens_a_second_editor(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})
    again = editor.open_beside_stage(cfg, "/x/b.md", which=only_nano, env={})
    assert again["action"] == "focused" and "already open" in again["message"]
    assert len(fake.named("split-window")) == 1


def test_no_editor_installed_opens_nothing_and_says_why(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    result = editor.open_beside_stage(cfg_for(tmp_path), "/x/a.md", which=lambda n: None, env={})
    assert result == {"ok": False, "action": "none", "message": editor.NOT_FOUND}
    assert not fake.named("split-window")


def test_a_refused_split_changes_nothing_and_says_so(tmp_path, monkeypatch):
    fake = EditorTmux()
    fake.refuse.add("split-window")
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    result = editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})
    assert not result["ok"] and "nothing changed" in result["message"]
    assert editor.where(cfg) is None


def test_a_narrow_window_gets_the_editor_in_its_own_window(tmp_path, monkeypatch):
    fake = EditorTmux(width=65)
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    result = editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})
    assert result["action"] == "window" and "own window (EDIT)" in result["message"]
    assert fake.windows[0]["panes"] == ["%0"]
    assert any(w["name"] == "EDIT" for w in fake.windows.values())


def test_the_configured_editor_is_what_opens(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path, "nvim")
    which = lambda n: f"/usr/bin/{n}" if n in ("nvim", "nano") else None
    editor.open_beside_stage(cfg, "/x/a.md", line=7, which=which, env={})
    assert fake.shells[-1] == "/usr/bin/nvim +7 /x/a.md"


# ------------------------------------------------------------------ restart / quit / back


def test_release_all_parks_the_editor_with_its_text_never_kills_it(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    stage.stage(cfg, 4)
    pane = editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})["pane_id"]
    result = stage.release_all(cfg)
    assert result["ok"]
    assert fake.windows[0]["panes"] == ["%0"]                 # window 0 is the deck alone again
    parked = [w for w in fake.windows.values() if pane in w["panes"]]
    assert parked and parked[0]["name"] == "EDIT"
    assert "with your text" in result["message"] and "loom-os is back" in result["message"]
    assert not fake.named("kill-pane")
    assert editor.where(cfg)["beside"] is False               # still open, just parked


def test_e_brings_a_parked_editor_back_beside_the_list(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    pane = editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})["pane_id"]
    editor.park(cfg)
    back = editor.open_beside_stage(cfg, which=only_nano, env={})
    assert back["action"] == "returned" and back["pane_id"] == pane
    assert pane in fake.windows[0]["panes"]
    assert len(fake.named("split-window")) == 1               # the same editor, not a new one


def test_cli_release_moves_the_editor_out_before_a_restart(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(stage, "_cli_cfg", lambda: cfg)
    editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})
    assert stage.main(["release"]) == 0
    assert fake.windows[0]["panes"] == ["%0"]


def test_an_editor_that_will_not_move_stops_the_restart(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(stage, "_cli_cfg", lambda: cfg)
    editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})
    fake.refuse.add("break-pane")
    assert stage.main(["release"]) == stage.EXIT_STILL_STAGED
    assert not stage.release_all(cfg)["ok"]
    assert "would not move the editor" in stage.release_all(cfg)["message"]


def test_staging_a_session_while_the_editor_is_open_keeps_the_editor(tmp_path, monkeypatch):
    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    pane = editor.open_beside_stage(cfg, "/x/a.md", which=only_nano, env={})["pane_id"]
    stage.stage(cfg, 4)
    stage.stage(cfg, 5)          # switching sessions puts loom-os back; the editor stays
    assert pane in fake.windows[0]["panes"] and "%5" in fake.windows[0]["panes"]
    editor.rebalance(cfg)
    assert fake.named("resize-pane")[-1][1:3] == ("-t", "%5")


def test_the_restart_everything_question_names_an_open_editor(tmp_path, monkeypatch):
    from pantheon import restart

    fake = EditorTmux()
    _install(monkeypatch, fake)
    cfg = cfg_for(tmp_path)
    assert restart._editor_note(cfg) == ""
    editor.open_beside_stage(cfg, "/x/plan.md", which=only_nano, env={})
    assert restart._editor_note(cfg) == "; your open editor (plan.md) closes too -- save it first"
