"""The Base filter-expression language, in isolation: `pantheon/tasks/basefilter.py`
never touches a file, so every test here builds a `Node`/`ViewSpec` straight from a Python dict or
string, the same shapes `yaml.safe_load` would hand back from a real `.base` file.

The parity check at the bottom is the load-bearing one: it runs the real `tests/fixtures/Sprints.base`
through `basefilter.parse_view` + `basefilter.to_tabspec` and compares every view against the
hand-copied rules in `pantheon/queue/filters.py` -- the same cross-check `tests/test_vault.py`
already does through `ObsidianBaseSource`, now proved at the pure-function layer directly.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import frontmatter
import pytest

from pantheon.models import TaskRow
from pantheon.queue import filters as spec_filters
from pantheon.tasks import basefilter as bf
from pantheon.tasks.obsidian_base import read_base_views, row_from_post

FIXTURES = Path(__file__).parent / "fixtures"


def row(**kwargs) -> TaskRow:
    kwargs.setdefault("id", "x")
    return TaskRow(**kwargs)


# ---------------------------------------------------------------- comparisons


def test_equality_and_inequality():
    keep = bf.compile_rule('status != "done"')
    assert keep(row(status="Done")) is False       # case-insensitive
    assert keep(row(status="open")) is True
    is_next = bf.compile_rule("next == true")
    assert is_next(row(next=True)) is True
    assert is_next(row()) is False


def test_ordering_operators_prefer_numbers_when_both_sides_parse():
    """`count` is not a real Sprints field -- it lives in `extra`, exercising a plain numeric
    comparison the same way `formula.age` or a the user-added property would."""
    less = bf.compile_rule("count < 5")
    assert less(row(extra={"count": "3"})) is True
    assert less(row(extra={"count": "9"})) is False
    at_most = bf.compile_rule("count <= 3")
    assert at_most(row(extra={"count": "3"})) is True
    more = bf.compile_rule("count > 3")
    assert more(row(extra={"count": "9"})) is True
    at_least = bf.compile_rule("count >= 9")
    assert at_least(row(extra={"count": "9"})) is True


def test_ordering_operators_fall_back_to_text_for_dates():
    """ISO dates sort correctly as plain text (already zero-padded), so `<`/`>` work on them
    without a date parser."""
    before = bf.compile_rule('created < "2026-06-01"')
    assert before(row(created="2026-01-15")) is True
    assert before(row(created="2026-08-05")) is False


def test_file_in_folder_is_always_true():
    keep = bf.compile_rule('file.inFolder("Projects/Sprints")')
    assert keep(row()) is True


def test_file_has_tag_matches_case_and_hash_insensitively():
    keep = bf.compile_rule('file.hasTag("chore")')
    assert keep(row(extra={"tags": ["Chore", "garden"]})) is True
    assert keep(row(extra={"tags": ["#chore"]})) is True
    assert keep(row(extra={"tags": ["garden"]})) is False
    assert keep(row(extra={})) is False


def test_an_unknown_rule_raises_rather_than_guessing():
    with pytest.raises(bf.UnknownRule):
        bf.compile_rule("summary.contains('x')")


# ---------------------------------------------------------------- the tree: and/or/not


def test_and_requires_every_child():
    node = bf.parse_node({"and": ["done != true", "next == true"]})
    assert bf.evaluate(node, row(done=False, next=True)) is True
    assert bf.evaluate(node, row(done=False, next=False)) is False


def test_or_requires_only_one_child():
    node = bf.parse_node({"or": ["tier == \"now\"", "tier == \"soon\""]})
    assert bf.evaluate(node, row(tier="soon")) is True
    assert bf.evaluate(node, row(tier="someday")) is False


def test_not_inverts_its_child():
    node = bf.parse_node({"not": "done == true"})
    assert bf.evaluate(node, row(done=False)) is True
    assert bf.evaluate(node, row(done=True)) is False


def test_nested_or_inside_and():
    node = bf.parse_node({
        "and": [
            "done != true",
            {"or": ["tier == \"now\"", "agent == true"]},
        ]
    })
    assert bf.evaluate(node, row(done=False, tier="now", agent=False)) is True
    assert bf.evaluate(node, row(done=False, tier="soon", agent=True)) is True
    assert bf.evaluate(node, row(done=False, tier="soon", agent=False)) is False
    assert bf.evaluate(node, row(done=True, tier="now", agent=True)) is False


def test_a_bare_leaf_string_works_without_an_and_wrapper():
    node = bf.parse_node("next == true")
    assert bf.evaluate(node, row(next=True)) is True
    assert bf.evaluate(node, row(next=False)) is False


# ---------------------------------------------------------------- unknown -> flagged, never fatal


def test_an_unknown_leaf_is_permissive_and_recorded_rather_than_failing():
    node = bf.parse_node({"and": ["done != true", "summary.contains(\"x\")"]})
    # the row keeps whatever `done != true` says; the unrecognised line never excludes anyone
    assert bf.evaluate(node, row(done=False)) is True
    assert bf.collect_unknown(node) == ['summary.contains("x")']


def test_parse_view_surfaces_unknown_leaves_on_the_spec():
    view = bf.parse_view({
        "name": "Weird",
        "filters": {"and": ["done != true", "summary.contains(\"x\")"]},
    })
    assert view.unknown == ['summary.contains("x")']
    tab = bf.to_tabspec(view)
    assert tab.filter(row(done=False)) is True


# ---------------------------------------------------------------- parse_view / to_tabspec


def test_parse_view_reads_sort_and_group():
    view = bf.parse_view({
        "name": "Agent's plate",
        "filters": {"and": ["agent == true"]},
        "sort": [{"property": "tier", "direction": "DESC"}, {"property": "created"}],
        "groupBy": {"property": "est_context", "direction": "DESC"},
    })
    assert view.sort == [("tier", "DESC"), ("created", "ASC")]
    assert view.group_by == "est_context"


def test_to_tabspec_keeps_an_explicit_key_from_a_tabs_yaml_entry():
    view = bf.parse_view({"name": "Now", "key": "4", "filters": "next == true"})
    tab = bf.to_tabspec(view)
    assert tab.key == "4"
    assert tab.filter(row(next=True)) is True


def test_to_tabspec_leaves_a_missing_key_blank_for_the_caller_to_assign():
    view = bf.parse_view({"name": "Now", "filters": "next == true"})
    tab = bf.to_tabspec(view)
    assert tab.key == ""


def test_to_tabspec_group_by_falls_back_to_none_heading():
    view = bf.parse_view({
        "name": "By project", "filters": "done != true",
        "groupBy": {"property": "project"},
    })
    tab = bf.to_tabspec(view)
    assert tab.group_by(row(project="garden")) == "garden"
    assert tab.group_by(row(project=None)) == "none"


# ---------------------------------------------------------------- sort_key_for


def test_sort_key_for_orders_blanks_last_regardless_of_direction():
    key = bf.sort_key_for([("picked", "ASC")])
    rows = [row(id="a", picked=None, summary="a"), row(id="b", picked="2026-01-01", summary="b")]
    assert sorted(rows, key=key)[0].id == "b"


def test_sort_key_for_defaults_to_created_ascending_with_no_sort_list():
    key = bf.sort_key_for([])
    rows = [row(id="a", created="2026-02-01"), row(id="b", created="2026-01-01")]
    assert [r.id for r in sorted(rows, key=key)] == ["b", "a"]


# ---------------------------------------------------------------- first_in_folder


def test_first_in_folder_finds_the_first_statement_in_view_order():
    views = [
        {"name": "A", "filters": {"and": ["done != true"]}},
        {"name": "B", "filters": {"and": ['file.inFolder("Projects/Sprints")', "next == true"]}},
    ]
    assert bf.first_in_folder(views) == "Projects/Sprints"


def test_first_in_folder_is_none_when_no_view_has_one():
    views = [{"name": "A", "filters": {"and": ["done != true"]}}]
    assert bf.first_in_folder(views) is None


def test_first_in_folder_on_the_real_fixture_base():
    views = read_base_views(FIXTURES / "Sprints.base")
    assert bf.first_in_folder(views) == "Projects/Sprints"


# ---------------------------------------------------------------- parity with queue/filters.py


def _fixture_rows() -> list[TaskRow]:
    return [
        row_from_post(path, frontmatter.loads(path.read_text(encoding="utf-8")))
        for path in sorted((FIXTURES / "sprints").glob("*.md"))
    ]


def test_the_real_base_file_has_eight_views():
    views = read_base_views(FIXTURES / "Sprints.base")
    assert len(views) == 8


def test_every_named_view_agrees_with_queue_filters_on_the_fixtures():
    rows = _fixture_rows()
    views = read_base_views(FIXTURES / "Sprints.base")
    spec_counts = spec_filters.counts(rows)
    disagreements = []
    for raw in views:
        view = bf.parse_view(raw)
        if view.name not in spec_counts:
            continue        # "My queue": a view the specs never named
        tab = bf.to_tabspec(view)
        from_base = {r.id for r in rows if tab.filter(r)}
        from_spec = {r.id for r in spec_filters.select(rows, view.name)}
        if from_base != from_spec:
            disagreements.append((view.name, len(from_base), len(from_spec),
                                  sorted(from_base ^ from_spec)))
    assert not disagreements, f"basefilter and queue/filters.py disagree: {disagreements}"
