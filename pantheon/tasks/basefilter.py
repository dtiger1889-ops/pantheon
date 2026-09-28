"""Obsidian's Base filter-expression language, as pure functions with no file I/O.

Extracted out of `pantheon/tasks/obsidian_base.py`, which read exactly this grammar inline, so a
second source -- the standalone folder's own `pantheon-tabs.yaml` -- can use the
identical filter language without either source importing the other. `obsidian_base.py` now
imports from here; nothing about what it reads out of a `.base` file changed (`tests/test_vault.py`
proves that), and `tests/test_basefilter.py` exercises this module directly.

What a Base view's `filters:` block looks like, confirmed against the real `Sprints.base`
:

    filters:
      and:
        - file.inFolder("Projects/Sprints")
        - done != true
        - status != "done"

a recursive tree of `and` / `or` / `not`, each holding nested trees or one-line statements. A
statement is either a comparison (`== != < <= > >=` between a property name and a literal) or one
of the two functions Pantheon understands (`file.inFolder(...)`, `file.hasTag(...)`). Anything else
-- a function this file does not know, or text that does not parse as a statement at all -- is
never fatal: it is treated as "no rule" (always true, so a tab only ever shows MORE rows than
Obsidian, never fewer) and recorded so the caller can mark that tab with a `*` on screen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..models import TaskRow
from .base import TabSpec

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


class UnknownRule(ValueError):
    """One line of a Base view we do not know how to reproduce."""


# ---------------------------------------------------------------- value tidying


def as_bool(value: Any) -> Optional[bool]:
    """`true` / `"true"` / `"yes"` all mean yes. Blank or anything odd means "not set"."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


# ---------------------------------------------------------------- the tree


@dataclass
class Node:
    """One node of a parsed `filters:` tree: a boolean combinator (`and`/`or`/`not`, with children)
    or a leaf test. `text` and `unknown` are only meaningful on a leaf: `text` is the original
    one-line statement (used to name the folder in `first_in_folder`, and to log what was
    skipped), `unknown` is True when `test` is the permissive "always true" fallback because this
    module could not read that line."""

    kind: str                              # "and" | "or" | "not" | "test"
    children: tuple["Node", ...] = ()
    test: Optional[Callable[[TaskRow], bool]] = None
    text: str = ""
    unknown: bool = False


def evaluate(node: Node, row: TaskRow) -> bool:
    """Walk a parsed filter tree against one row."""
    if node.kind == "and":
        return all(evaluate(c, row) for c in node.children)
    if node.kind == "or":
        return any(evaluate(c, row) for c in node.children)
    if node.kind == "not":
        return not evaluate(node.children[0], row)
    return node.test(row)  # kind == "test"


def collect_unknown(node: Node) -> list[str]:
    """Every leaf's original text where this module had to fall back to "always true"."""
    if node.kind == "test":
        return [node.text] if node.unknown else []
    out: list[str] = []
    for child in node.children:
        out.extend(collect_unknown(child))
    return out


def _always_true(text: str, unknown: bool = False) -> Node:
    return Node(kind="test", test=lambda row: True, text=text, unknown=unknown)


def parse_node(block: Any) -> Node:
    """One node of a `filters:` block: a leaf statement (a string), a nested `and`/`or`/`not`, or
    -- for headroom, since a Base view could in principle write one -- a bare list, read the same
    way `and` is. Anything else (missing, or a shape we do not recognise) means "no rule": every
    row passes, same as an empty `and` list would."""
    if isinstance(block, str):
        return _leaf_node(block)
    if isinstance(block, list):
        return Node(kind="and", children=tuple(parse_node(b) for b in block))
    if isinstance(block, dict):
        if "and" in block:
            return Node(kind="and", children=tuple(parse_node(b) for b in (block["and"] or [])))
        if "or" in block:
            return Node(kind="or", children=tuple(parse_node(b) for b in (block["or"] or [])))
        if "not" in block:
            return Node(kind="not", children=(parse_node(block["not"]),))
    return _always_true(str(block) if block else "")


def _leaf_node(text: str) -> Node:
    try:
        test = compile_rule(text)
    except UnknownRule:
        return _always_true(text, unknown=True)
    return Node(kind="test", test=test, text=text)


# ---------------------------------------------------------------- one leaf statement


_IN_FOLDER = re.compile(r'^file\.inFolder\(\s*"([^"]*)"\s*\)$')
_HAS_TAG = re.compile(r'^file\.hasTag\(\s*"([^"]*)"\s*\)$')
_COMPARISON = re.compile(r'^([A-Za-z_][\w.]*)\s*(==|!=|<=|>=|<|>)\s*(.+)$')


def _literal(token: str) -> Any:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    low = token.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none", "empty"):
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    raise UnknownRule(token)


def _property(row: TaskRow, name: str) -> Any:
    """Look a property up on a row. `note.summary`-style names use the last part, which is how
    Obsidian addresses a property on the note itself."""
    key = name.split(".")[-1]
    if hasattr(row, key):
        return getattr(row, key)
    return row.extra.get(key)


def _has_tag(row: TaskRow, tag: str) -> bool:
    """`file.hasTag("x")`: true when the note's frontmatter `tags` list contains `x`, matched
    case-insensitively and ignoring a leading `#` on either side (Obsidian writes tags both ways).
    Not used by the real Sprints Base today; built for the headroom the research called for."""
    wanted = tag.strip().lstrip("#").lower()
    raw = row.extra.get("tags")
    values = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    return any(str(v).strip().lstrip("#").lower() == wanted for v in values)


def _orderable(value: Any) -> Any:
    """A value `<`/`<=`/`>`/`>=` can compare: a number when both sides parse as one (so dates
    written as plain ISO text still order correctly, being already zero-padded), else lower-cased
    text. Booleans and blanks are not orderable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text.lower() if text else None


def _compare(actual: Any, op: str, wanted: Any) -> bool:
    if op in ("==", "!="):
        if isinstance(wanted, bool):
            same = as_bool(actual) is wanted
        elif wanted is None:
            same = actual is None or str(actual).strip() == ""
        else:
            same = str(actual or "").strip().lower() == str(wanted).strip().lower()
        return same if op == "==" else not same
    left, right = _orderable(actual), _orderable(wanted)
    if left is None or right is None or not isinstance(left, type(right)):
        # Mixed number/text, or either side blank: fall back to a plain numeric attempt, else
        # this row is left out rather than guessed into place.
        try:
            left, right = float(actual), float(wanted)
        except (TypeError, ValueError):
            return False
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    return left >= right  # ">="


def compile_rule(expression: str) -> Callable[[TaskRow], bool]:
    """Turn one line of a Base view into a yes/no test. Raises `UnknownRule` if we cannot."""
    text = str(expression).strip()
    if _IN_FOLDER.match(text):
        # Every row a source hands this test already came from that folder, by construction.
        return lambda row: True
    match = _HAS_TAG.match(text)
    if match:
        tag = match.group(1)
        return lambda row, _t=tag: _has_tag(row, _t)
    match = _COMPARISON.match(text)
    if not match:
        raise UnknownRule(text)
    name, op, raw = match.groups()
    wanted = _literal(raw)
    return lambda row, _n=name, _o=op, _w=wanted: _compare(_property(row, _n), _o, _w)


# ---------------------------------------------------------------- sorting


class _Reverse:
    """Sorts backwards, so one field can descend while the others ascend."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Reverse) and self.value == other.value

    def __lt__(self, other: "_Reverse") -> bool:
        return self.value > other.value


def sort_key_for(sort: list[tuple[str, str]]) -> Callable[[TaskRow], object]:
    """Build a sort key out of a view's `sort:` list (already reduced to `(property, DIRECTION)`
    pairs by `parse_view`). Blank values always sort last, whichever direction is asked for."""
    fields = [(name, direction.upper().startswith("DESC")) for name, direction in sort] or [("created", False)]

    def key(row: TaskRow):
        parts: list[object] = []
        for name, descending in fields:
            value = _property(row, name)
            missing = value is None or str(value).strip() == ""
            if isinstance(value, bool):
                rank: object = (0 if value else 1) if descending else (1 if value else 0)
            else:
                text = str(value or "").lower()
                rank = _Reverse(text) if descending else text
            parts.append((missing, rank))
        parts.append(str(row.summary or "").lower())
        return tuple(parts)

    return key


# ---------------------------------------------------------------- one view


@dataclass
class ViewSpec:
    """One parsed view or tab, whichever source it came from: a `.base` file's `views[]` entry,
    or a standalone `pantheon-tabs.yaml` entry -- both are read by `parse_view`."""

    name: str
    filters: Node
    sort: list[tuple[str, str]] = field(default_factory=list)
    group_by: Optional[str] = None
    key: str = ""                                  # explicit tab key, e.g. from a tabs.yaml entry
    unknown: list[str] = field(default_factory=list)  # leaf statements this module could not read


def parse_view(view: dict) -> ViewSpec:
    """A `.base` view dict or a `pantheon-tabs.yaml` tab dict, either way. Both shapes carry
    `name`/`filters`/`sort`/`groupBy`; a tabs.yaml entry also carries its own `key`, which this
    keeps rather than inventing one (a Base view has no `key` at all -- the caller numbers those,
    same as before this extraction)."""
    name = str(view.get("name") or view.get("title") or "").strip()
    root = parse_node(view.get("filters") or {})
    sort: list[tuple[str, str]] = []
    for entry in (view.get("sort") or []):
        if isinstance(entry, dict) and entry.get("property"):
            sort.append((str(entry["property"]), str(entry.get("direction", "ASC")).upper()))
    group_by = None
    grouping = view.get("groupBy")
    if isinstance(grouping, dict) and grouping.get("property"):
        group_by = str(grouping["property"])
    return ViewSpec(
        name=name,
        filters=root,
        sort=sort,
        group_by=group_by,
        key=str(view.get("key") or ""),
        unknown=collect_unknown(root),
    )


def to_tabspec(view: ViewSpec) -> TabSpec:
    """The default rendering of a parsed view as a `TabSpec`: keeps whatever key the view already
    carried (a tabs.yaml entry writes its own; a Base view has none, so this leaves it blank for
    the caller to assign, exactly as `obsidian_base.py` already did). Grouping falls back to plain
    alphabetical headings -- a Base cannot express a heading ORDER, only a property and a
    direction -- which is why `ObsidianBaseSource` still overrides the Agent's-plate's own heading
    order from the hand-written spec after calling this."""
    filters = view.filters
    group_by = None
    if view.group_by:
        prop = view.group_by
        group_by = lambda row, _p=prop: str(_property(row, _p) or "").strip() or "none"
    return TabSpec(
        name=view.name,
        key=view.key,
        filter=lambda row, _n=filters: evaluate(_n, row),
        sort_key=sort_key_for(view.sort),
        group_by=group_by,
        group_order=[],
    )


def first_in_folder(views_yaml: list[dict]) -> Optional[str]:
    """The folder named by the first `file.inFolder("...")` filter across these views' filter
    trees, in view order then depth-first tree order. Used to derive `sprints_folder` when it is
    not set in `pantheon.toml`: the Base's own filter is the vault-relative source of
    truth, so a second Base naming a different folder just works."""
    for view in views_yaml:
        found = _first_in_folder_node(parse_node(view.get("filters") or {}))
        if found is not None:
            return found
    return None


def _first_in_folder_node(node: Node) -> Optional[str]:
    if node.kind == "test":
        match = _IN_FOLDER.match(node.text.strip())
        return match.group(1) if match else None
    for child in node.children:
        found = _first_in_folder_node(child)
        if found is not None:
            return found
    return None
