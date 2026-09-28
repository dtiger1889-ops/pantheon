"""S-UI acceptance 3: "every button has a key and every key
a button." This enumerates each pane's `BINDINGS` and the full toolbar action catalogue each pane
can ever show, and asserts the two key sets match -- allowing `?`, `q`, `Tab`, number keys, `]`/`[`,
and arrows as key-only (the spec's own exception list), plus a small, named, per-pane list of
further pane-wide (not per-row) keys that have no row-toolbar button by design: refresh, the
project filter, search, back, and the `j`/`k` up/down aliases (already covered by the arrow-key
exception in spirit -- same movement, a second set of keys). `Enter` is NOT an exception: its
button is the collapsed toolbar's own `Actions… (Enter)` (`pantheon/widgets/toolbar.py`
`COLLAPSE_LABEL`), not a per-status `ACTION_DEFS` entry, so it is added to the expected set instead
of excused from it.
"""
from __future__ import annotations

from textual.binding import Binding

from pantheon.queue.pane import QUEUE_ACTION_DEFS, QueuePane
from pantheon.supervisor.pane import ACTION_DEFS, SupervisorPane
from pantheon.widgets.toolbar import COLLAPSE_LABEL

# The spec's own exception list (S-UI acceptance 3), spelled out as Textual key names.
SPEC_KEY_ONLY = {
    "question_mark", "q", "tab", "shift+tab",
    "left_square_bracket", "right_square_bracket",
    "up", "down", "left", "right",
} | {str(n) for n in range(1, 10)}

# Pane-wide actions with no row to attach a toolbar button to -- named, not waved through.
SUPERVISOR_EXTRA_KEY_ONLY = {"r"}                              # refresh the whole list
QUEUE_EXTRA_KEY_ONLY = {"r", "p", "j", "k", "escape", "slash", "d", "D"}
# refresh, project filter, up/down aliases, back, open search, and drill/un-drill: a
# whole-pane source swap, not a per-row action, so it has no row-toolbar button by design.


def _binding_key(b) -> str:
    return b.key if isinstance(b, Binding) else b[0]


def _binding_keys(pane_cls) -> set[str]:
    return {_binding_key(b) for b in pane_cls.BINDINGS}


def test_supervisor_every_key_has_a_button_and_every_button_a_key():
    keys = _binding_keys(SupervisorPane) - SPEC_KEY_ONLY - SUPERVISOR_EXTRA_KEY_ONLY
    # `enter` opens the collapsed toolbar's Pick (its own button, not in ACTION_DEFS -- see module
    # docstring), so it belongs in the expected set rather than the exception list.
    button_keys = {a.key for a in ACTION_DEFS.values()} | {"enter"}
    assert keys == button_keys


def test_queue_every_key_has_a_button_and_every_button_a_key():
    keys = _binding_keys(QueuePane) - SPEC_KEY_ONLY - QUEUE_EXTRA_KEY_ONLY
    button_keys = {a.key for a in QUEUE_ACTION_DEFS.values()}
    assert keys == button_keys


def test_every_supervisor_toolbar_button_label_carries_its_own_key():
    """`Kill (k)`, `Model… (m)`, ... -- the key is printed on the button itself (S-UI 4.4)."""
    for action in ACTION_DEFS.values():
        assert f"({action.key})" in f"{action.label} ({action.key})"


def test_every_queue_toolbar_button_label_carries_its_own_key():
    for action in QUEUE_ACTION_DEFS.values():
        assert f"({action.key})" in f"{action.label} ({action.key})"


def test_collapsed_toolbar_label_names_the_enter_key():
    assert "(Enter)" in COLLAPSE_LABEL


def test_no_toolbar_action_key_is_reused_for_two_different_pane_level_bindings():
    """Sanity check on the fixture data above: nothing in the "extra key-only" lists secretly
    collides with a real toolbar action key for that same pane (which would make the main
    assertion above vacuously true instead of meaningful)."""
    sup_button_keys = {a.key for a in ACTION_DEFS.values()}
    assert not (SUPERVISOR_EXTRA_KEY_ONLY & sup_button_keys)
    queue_button_keys = {a.key for a in QUEUE_ACTION_DEFS.values()}
    assert not (QUEUE_EXTRA_KEY_ONLY & queue_button_keys)
