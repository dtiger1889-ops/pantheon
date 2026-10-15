"""No subprocess Pantheon starts may inherit the terminal's input.

Why (2026-09-30): under MSYS2 every tmux client that inherits the pane's terminal input leaves a
mutex behind in the tmux server (100 inherited-input queries added 100; 500 with null input added
none), and that leak ended in the freezes. So every subprocess call inside `pantheon/` must say
what its input is (`stdin=subprocess.DEVNULL`, a pipe, `input=`), and `bin/pantheon` must send
every tmux command through its `tmux()` helper, which adds `</dev/null`.

The Python check reads source with `ast` and runs nothing. It catches `subprocess.run(...)`,
module aliases (`import subprocess as sp`) and imported names (`from subprocess import run`).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "pantheon"
LAUNCHER = ROOT / "bin" / "pantheon"

CALLS = {"run", "Popen", "call", "check_call", "check_output"}

# {("pantheon/relative/path.py", "enclosing function"): "why this call needs the keyboard"}.
# The interactive tmux attaches are not here; they live in bin/pantheon (checked below).
ALLOWED: dict[tuple[str, str], str] = {
    ("pantheon/queue/pane.py", "_open_in_editor"):
        "runs $EDITOR in the foreground while the deck is suspended; a terminal editor "
        "(vim, nano) reads the keyboard, so it must get the real terminal",
}


def _subprocess_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names that mean the subprocess module, and names bound straight to its functions."""
    modules, funcs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in CALLS:
                    funcs.add(alias.asname or alias.name)
    return modules, funcs


def _calls_without_stdin(path: Path) -> list[tuple[int, str]]:
    """(line, enclosing function) for each subprocess call that does not pass `stdin=` or `input=`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules, funcs = _subprocess_names(tree)
    found = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call):
            target = node.func
            is_subprocess = (
                (isinstance(target, ast.Attribute) and target.attr in CALLS
                 and isinstance(target.value, ast.Name) and target.value.id in modules)
                or (isinstance(target, ast.Name) and target.id in funcs))
            # `input=` also sets the child's input (run and check_output pipe it in).
            if is_subprocess and not any(k.arg in ("stdin", "input") for k in node.keywords):
                found.append((node.lineno, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def test_every_subprocess_call_in_pantheon_sets_its_input():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for line, function in _calls_without_stdin(path):
            if (rel, function) not in ALLOWED:
                offenders.append(f"{rel}:{line} ({function})")
    assert not offenders, (
        "subprocess calls that inherit the terminal's input -- pass stdin=subprocess.DEVNULL "
        "(or add an ALLOWED entry saying why the call needs the keyboard):\n  "
        + "\n  ".join(offenders))


def test_allowlist_entries_still_exist():
    # A stale entry would quietly allow a future call in a function of the same name.
    live = set()
    for path in PACKAGE.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        live.update((rel, function) for _, function in _calls_without_stdin(path))
    assert not set(ALLOWED) - live, f"ALLOWED entries with no matching call: {set(ALLOWED) - live}"


def _code_lines(path: Path) -> list[tuple[int, str]]:
    return [(n, line) for n, line in enumerate(path.read_text().splitlines(), 1)
            if line.strip() and not line.lstrip().startswith("#")]


# The two places `command tmux` is allowed in bin/pantheon, matched by their text, not line numbers:
# the `tmux()` helper itself, and the interactive attaches, which need the real keyboard.
HELPER_START = re.compile(r"^tmux\(\)\s*\{")
ATTACH = re.compile(r"^command tmux attach -t ")


def _helper_lines(lines):
    """Line numbers inside the `tmux()` helper (one line, or a block closed by a lone `}`)."""
    inside, out = False, set()
    for n, line in lines:
        text = line.strip()
        if not inside and HELPER_START.match(text):
            out.add(n)
            if text.endswith("}") and text.count("{") == text.count("}"):
                continue
            inside = True
        elif inside:
            out.add(n)
            if text == "}":
                inside = False
    return out


def test_launcher_runs_tmux_only_through_its_helper_or_an_attach():
    lines = list(_code_lines(LAUNCHER))
    helper = _helper_lines(lines)
    assert helper, "bin/pantheon no longer defines the tmux() helper that adds </dev/null"
    offenders = [f"bin/pantheon:{n}: {line.strip()}" for n, line in lines
                 if "command tmux" in line
                 and not (n in helper and "</dev/null" in line)
                 and not ATTACH.match(line.strip())]
    assert not offenders, (
        "`command tmux` bypasses the </dev/null helper; use plain `tmux`:\n  " + "\n  ".join(offenders))
    assert any("command tmux" in line and "</dev/null" in line for n, line in lines if n in helper), \
        "the tmux() helper no longer gives tmux </dev/null"


# A tmux command in a script: at the start of a line or after ; & | ( or $( , optionally via `command`.
TMUX_CALL = re.compile(r"(^|[;&|(]|\$\()\s*(command\s+)?tmux\s")


def test_other_bin_scripts_give_tmux_no_terminal_input():
    offenders = []
    for path in sorted((ROOT / "bin").iterdir()):
        if path == LAUNCHER or not path.is_file() or path.suffix in (".py", ".ico"):
            continue
        try:
            head = path.read_text().splitlines()[:1]
        except UnicodeDecodeError:
            continue
        if not head or not head[0].startswith("#!") or "sh" not in head[0]:
            continue
        offenders += [f"bin/{path.name}:{n}: {line.strip()}" for n, line in _code_lines(path)
                      if TMUX_CALL.search(line) and "</dev/null" not in line]
    assert not offenders, "tmux calls without </dev/null:\n  " + "\n  ".join(offenders)
