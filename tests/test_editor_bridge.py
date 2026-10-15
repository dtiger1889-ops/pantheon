"""`V` opens a file or folder in VS Code through the Scheduled Task bridge. In-process
only: nothing here runs schtasks or VS Code (the runner is a fake)."""
from __future__ import annotations

import json
import os

from pantheon import editor_bridge as eb

CODE = r"C:\Users\x\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd"


class FakeRun:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return type("Done", (), {"returncode": self.returncode})()


def only(path):
    """An `exists` that knows exactly one file, whichever slash form it is asked in."""
    return lambda p: eb.to_windows(p) == path


def test_path_forms_round_trip():
    assert eb.to_windows("/c/Users/x/proj") == r"C:\Users\x\proj"
    assert eb.to_windows("C:/Users/x/a.md") == r"C:\Users\x\a.md"
    assert eb.to_windows("/c") == "C:\\"
    assert eb.to_windows("/tmp/x") == "/tmp/x"
    if os.name == "posix":
        assert eb.to_posix(r"C:\Users\x\a.md") == "/c/Users/x/a.md"


def test_build_command_for_a_file_a_line_and_a_folder():
    assert eb.build_command(CODE, "/c/p/a.py") == [CODE, "--reuse-window", r"C:\p\a.py"]
    assert eb.build_command(CODE, "/c/p/a.py", line=12) == [
        CODE, "--reuse-window", "--goto", r"C:\p\a.py:12"]
    assert eb.build_command(CODE, "C:/p", is_dir=True) == [CODE, r"C:\p"]


def test_cli_comes_from_config_then_path_then_the_install_folder():
    custom = r"D:\tools\code.cmd"
    assert eb.find_code_cli({"vscode_cli": custom}, env={}, which=lambda n: None,
                            exists=only(custom)) == custom
    assert eb.find_code_cli({}, env={}, which=lambda n: "/c/bin/code.cmd" if n == "code.cmd" else None,
                            exists=only(r"C:\bin\code.cmd")) == r"C:\bin\code.cmd"
    env = {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}
    assert eb.find_code_cli({}, env=env, which=lambda n: None, exists=only(CODE)) == CODE
    env = {"USERPROFILE": "C:\\Users\\x"}
    assert eb.find_code_cli({}, env=env, which=lambda n: None, exists=only(CODE)) == CODE


def test_missing_cli_says_so_in_plain_words_and_fires_nothing(tmp_path):
    run = FakeRun()
    result = eb.request_open({}, str(tmp_path / "state"), str(tmp_path), run=run, env={},
                             which=lambda n: None, exists=lambda p: False)
    assert result == {"ok": False, "message": eb.NO_CLI}
    assert "code" in result["message"] and "Traceback" not in result["message"]
    assert run.calls == []
    assert not (tmp_path / "state" / "editor" / "pending_open.json").exists()


def test_open_writes_the_command_and_runs_the_task(tmp_path):
    run = FakeRun(returncode=0)
    folder = tmp_path / "proj"
    folder.mkdir()
    state = tmp_path / "state"
    result = eb.request_open({"vscode_cli": CODE}, str(state), str(folder), run=run, env={},
                             which=lambda n: None, exists=only(CODE))
    assert result["ok"] is True and "VS Code" in result["message"]
    pending = json.loads((state / "editor" / "pending_open.json").read_text(encoding="utf-8"))
    assert pending["argv"][0] == CODE and len(pending["argv"]) == 2  # a folder: its own window
    assert pending["argv"][1].endswith("proj")
    (argv, kwargs), = run.calls
    assert argv[1:] == ["/run", "/tn", eb.TASK_NAME]
    assert kwargs["env"]["MSYS2_ARG_CONV_EXCL"] == "*" and kwargs["timeout"]


def test_task_not_registered_points_at_the_setup_doc(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("x", encoding="utf-8")
    result = eb.request_open({"vscode_cli": CODE}, str(tmp_path / "state"), str(note),
                             run=FakeRun(returncode=1), env={}, which=lambda n: None,
                             exists=only(CODE))
    assert result == {"ok": False, "message": eb.NOT_SET_UP}
    assert eb.SETUP_DOC in result["message"]


def test_nothing_selected_or_a_missing_path_is_a_plain_line(tmp_path):
    run = FakeRun()
    assert eb.request_open({}, str(tmp_path), "", run=run)["message"] == eb.NO_PATH
    gone = eb.request_open({}, str(tmp_path), str(tmp_path / "gone.md"), run=run)
    assert gone["ok"] is False and "isn't there" in gone["message"]
    assert run.calls == []
