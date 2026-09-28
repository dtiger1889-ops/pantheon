"""section 4 ("collect all user-facing text"): a read-only inventory, not a refactor. Rather
than moving every sentence in the package into a `strings.py` module (a much bigger change than
this pass calls for), this walks every `.py` file under `pantheon/` with the standard-library
`ast` module and prints every string that reaches the user's screen -- a `say` / `_say` /
`notify` call, a `message=` keyword argument, a `LaunchResult(...)` construction, and the
multi-line `KEYS_TEXT` / `HELP_TEXT` constants -- with its file:line, so a copy review (the
`app-interface-writing` skill) can read every sentence in one place instead of hunting through the
package one module at a time. The review itself is a separate pass; this only gathers the list.

    python -m pantheon.strings_inventory                        print the inventory
    python -m pantheon.strings_inventory > strings.md   save one run of it

`ast` (not a regex) reads the actual call graph, so an f-string, a multi-line concatenation, or a
message built from a keyword argument are all found the same way; `ast.unparse` reconstructs the
source text of each one, `{placeholder}` and all, exactly as it appears in the file.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, Optional

PACKAGE_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PACKAGE_ROOT.parent

# `say`/`_say` cover every pane's status-line sentence (`SupervisorPane.say`, `QueuePane._say`,
# `DeckApp._say`); `notify` is here for whatever eventually calls Textual's own toast API or a
# future `notify.channels` entry point, even though nothing does yet.
CALL_NAMES = {"say", "_say", "notify"}
CONSTRUCTOR_NAMES = {"LaunchResult"}
# Substrings, not exact names, so `KEYS_TEXT` (pantheon/keys.py and pantheon/queue/pane.py both
# have one) and any local `..._KEYS` alias are all caught, not just a constant named exactly this.
CONSTANT_NAME_MARKERS = ("KEYS", "HELP_TEXT")


def _call_name(node: ast.Call) -> Optional[str]:
    """`self.say(...)` -> "say"; `LaunchResult(...)` -> "LaunchResult"; anything else -> None."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _text_of(node: ast.AST) -> str:
    """The source text of one value node, f-strings and concatenations included -- `ast.unparse`
    (3.9+) reconstructs it faithfully, so a `{window_index}` placeholder reads exactly as written,
    not as a resolved value (there is nothing to resolve without running the program)."""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive: one odd node must never kill the whole run
        return "<could not read this value>"


def _string_lines(node: ast.AST) -> list[str]:
    """A `KEYS_TEXT`/`HELP_TEXT`-style triple-quoted constant, split into its real lines with the
    blank ones dropped. Not a plain string constant (a one-line message the caller passes through
    `say()` already gets picked up there) -- only the multi-line blocks this pass is meant to
    surface as more than one sentence."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "\n" in node.value:
        return [ln.strip() for ln in node.value.splitlines() if ln.strip()]
    return []


def _resolve_text(node: ast.AST, constants: dict[str, str]) -> Optional[str]:
    """`pantheon/keys.py`'s `KEYS_TEXT = "\\n\\n".join((ANYWHERE, DECK, QUEUE, HUD))` is not itself
    a string constant, so `_string_lines` would otherwise skip straight past the one line in the
    file that actually names every key's description. Resolve the common shapes that build a
    constant out of other module-level string constants -- a `"sep".join((...))` call and a `+`
    concatenation -- one level deep, using `constants` (every plain string assignment already seen
    in this file). Anything stranger than that falls back to `_text_of` in the caller, same as
    before this existed."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_text(node.left, constants)
        right = _resolve_text(node.right, constants)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join":
        sep = _resolve_text(node.func.value, constants)
        items = node.args[0] if node.args else None
        if sep is not None and isinstance(items, (ast.Tuple, ast.List)):
            parts = [_resolve_text(el, constants) for el in items.elts]
            if all(p is not None for p in parts):
                return sep.join(parts)  # type: ignore[arg-type]
    return None


class Finding:
    def __init__(self, path: Path, line: int, kind: str, text: str) -> None:
        self.path = path
        self.line = line
        self.kind = kind
        self.text = text

    def __str__(self) -> str:
        try:
            rel = self.path.relative_to(WORKSPACE_ROOT).as_posix()
        except ValueError:  # a path outside the workspace (a test fixture, say) -- print it as-is
            rel = self.path.as_posix()
        return f"{rel}:{self.line}  [{self.kind}]  {self.text}"


def _message_keyword(node: ast.Call) -> Optional[ast.AST]:
    return next((kw.value for kw in node.keywords if kw.arg == "message"), None)


def _scan_call(path: Path, node: ast.Call, constants: dict[str, str]) -> Optional[Finding]:
    name = _call_name(node)
    message = _message_keyword(node)

    def rendered(value_node: ast.AST) -> str:
        # `LaunchResult(message=WAITING_ON_GO)` should show the actual sentence, not the variable
        # name -- resolve a plain module-level constant reference; an f-string or a `.reason`
        # attribute access is left as its own source text, since there is nothing to resolve
        # without running the program.
        resolved = _resolve_text(value_node, constants)
        return resolved if resolved is not None else _text_of(value_node)

    if name in CALL_NAMES:
        if node.args:
            text = rendered(node.args[0])
        elif message is not None:
            text = rendered(message)
        else:
            text = "(no message argument -- check the call)"
        return Finding(path, node.lineno, name, text)
    if name in CONSTRUCTOR_NAMES:
        text = rendered(message) if message is not None else "(no message= given -- check the call)"
        return Finding(path, node.lineno, "LaunchResult", text)
    if message is not None:
        return Finding(path, node.lineno, f"{name or 'call'}(message=)", rendered(message))
    return None


def scan_file(path: Path) -> list[Finding]:
    """Every finding in one file, in the order `ast.walk` visits them (source order is not
    guaranteed by `ast.walk`, so callers sort by line before printing)."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Finding(path, 0, "error", f"could not read this file: {exc}")]
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "error", f"could not parse this file: {exc.msg}")]

    # Every plain-string module-level assignment, gathered first so a KEYS/HELP_TEXT constant
    # built out of them (`"\n\n".join((ANYWHERE, DECK, QUEUE, HUD))`) can be resolved below.
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any(marker in n for n in names for marker in CONSTANT_NAME_MARKERS):
                continue
            lines = _string_lines(node.value)
            if not lines:
                resolved = _resolve_text(node.value, constants)
                if resolved and "\n" in resolved:
                    lines = [ln.strip() for ln in resolved.splitlines() if ln.strip()]
            if lines:
                findings.extend(Finding(path, node.lineno, "constant", ln) for ln in lines)
            else:
                findings.append(Finding(path, node.lineno, "constant", _text_of(node.value)))
        elif isinstance(node, ast.Call):
            found = _scan_call(path, node, constants)
            if found is not None:
                findings.append(found)
    return findings


def scan_package(root: Path = PACKAGE_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        findings.extend(scan_file(path))
    findings.sort(key=lambda f: (str(f.path), f.line))
    return findings


def render(findings: Iterable[Finding]) -> str:
    findings = list(findings)
    lines = [
        "# User-facing strings inventory",
        "",
        "Generated by `python -m pantheon.strings_inventory`. Every `say()` / `_say()` / "
        "`notify()` call, `message=` keyword argument, `LaunchResult(...)` construction, and the "
        "`KEYS_TEXT` / `HELP_TEXT` constants in `pantheon/`, with its file:line. This is the raw "
        "material for a copy review -- nothing here is a "
        "verdict on the wording, and nothing in `pantheon/` was changed to produce it.",
        "",
        f"{len(findings)} findings.",
        "",
        "```",
    ]
    lines.extend(str(f) for f in findings)
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    print(render(scan_package()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
