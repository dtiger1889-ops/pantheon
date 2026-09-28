"""The row toolbar: buttons under the selected row, like Claude Desktop. A sibling `Horizontal` of real `Button`s, never inside the `DataTable` --
DataTable cells are Rich renderables, not a widget tree.

Since the buttons are one-row "pills" (no top/bottom chrome, a
filled background per variant, the primary one lit in the chrome colour) and the bar decides for
itself how much of the row to spend, from the width the pane hands it in `set_actions(width=...)`:

    full     `Kill (k)`     -- when every button fits this way
    compact  `Kill k`       -- when the full form does not fit
    bare     `Kill`         -- when even that does not fit (the key strip still lists the keys)
    collapsed               -- one `Actions… (Enter)` button: under 80 columns (the phone, P width;
                               S-UI 4.4 "P/N widths"), or when the bare form still does not fit

so the phone keeps its single big touch target and the desk never loses its buttons because a
panel happens to share the screen (the desk-first review, findings 3 and 14). Passing
`collapsed=` explicitly still works and overrides the width rule.

The pane that mounts this owns what happens when a button is pressed -- this widget only knows
"which action id was chosen" and reports that through the `dispatch` callback given at
construction, exactly as if the matching key had been pressed. That is the "keys and buttons share
one code path" rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Button

COLLAPSE_ID = "__actions__"
COLLAPSE_LABEL = "Actions… (Enter)"
COLLAPSE_AT = 80   # below this content width the bar is one button (the phone; S-UI 4.4)

PILL_PADDING = 2   # Button's own `line-pad: 1` either side of the label (its `padding` is 0 here)
PILL_GAP = 1       # `margin: 0 1 0 0`
BAR_PADDING = 2    # `padding: 0 1` on the bar itself


@dataclass(frozen=True)
class Action:
    """One toolbar button (and its key). `action_name` is both the `Button.id` and the name this
    widget hands back through `dispatch` -- the pane maps it straight onto its own `action_<name>`
    method, so a key press and a button press run the identical code."""

    key: str
    label: str
    action_name: str
    variant: str = "default"
    enabled: bool = True
    tooltip: Optional[str] = None


def label_for(action: Action, form: str) -> str:
    """The button text in one of the three forms: `full` (`Kill (k)`), `compact` (`Kill k`),
    `bare` (`Kill`)."""
    if form == "full":
        return f"{action.label} ({action.key})"
    if form == "compact":
        return f"{action.label} {action.key}"
    return action.label


def row_width(actions: list[Action], form: str) -> int:
    """How many cells a row of pills in this form takes: each pill is its text plus one cell of
    padding either side, with one cell between pills, inside the bar's own padding."""
    if not actions:
        return 0
    pills = sum(len(label_for(a, form)) + PILL_PADDING for a in actions)
    return pills + PILL_GAP * (len(actions) - 1) + BAR_PADDING


def choose_form(actions: list[Action], width: Optional[int],
                collapse_below: int = COLLAPSE_AT) -> Optional[str]:
    """Which form fits a bar this wide: `full`, `compact`, `bare`, or `None` meaning collapse.
    No width (`None`) means the caller did not measure; the full form is used, as before.
    `collapse_below` is the pane's own phone floor -- 80 for the agents panel, 66 for the queue,
    whose desk tier starts lower -- under which the bar is always one button."""
    if width is None:
        return "full"
    if width < collapse_below:
        return None
    for form in ("full", "compact", "bare"):
        if row_width(actions, form) <= width:
            return form
    return None


class RowToolbar(Horizontal):
    """Mounted once by a pane, under its table; shown only while a row is selected (S-UI 4.4,
    "Toolbar visibility... only under the selected row")."""

    # One-row pills. Textual's own Button is three rows tall with `tall` top/bottom borders per
    # variant and on hover; a plain `RowToolbar Button { border-top: none }` LOSES to Button's own
    # `Button.-primary { border-top: tall ... }` and the buttons paint as two rule lines with a
    # zero-height label, so the border rules carry `!important`.
    DEFAULT_CSS = """
    RowToolbar { height: auto; padding: 0 1; }
    RowToolbar Button {
        height: 1; min-width: 0; margin: 0 1 0 0; padding: 0;
        border: none !important; border-top: none !important; border-bottom: none !important;
        background: $surface-lighten-1; color: $foreground; text-style: none;
    }
    RowToolbar Button:hover { background: $primary-muted; }
    RowToolbar Button:focus { text-style: bold; background-tint: $foreground 8%; }
    RowToolbar Button.-active { background: $primary-muted; }
    RowToolbar Button.-primary { background: $primary-muted; color: $text-primary; text-style: bold; }
    RowToolbar Button.-primary:hover { background: $primary; color: $background; }
    RowToolbar Button.-primary.-active { background: $primary; color: $background; }
    RowToolbar Button.-error { background: $error-muted; color: $text-error; }
    RowToolbar Button.-error:hover { background: $error; color: $background; }
    RowToolbar Button.-error.-active { background: $error; color: $background; }
    RowToolbar Button:disabled { text-opacity: 0.45; }
    """

    actions: reactive[tuple[Action, ...]] = reactive(tuple, recompose=True)
    collapsed: reactive[bool] = reactive(False, recompose=True)
    form: reactive[str] = reactive("full", recompose=True)

    def __init__(self, dispatch: Callable[[str], None], id: Optional[str] = "toolbar") -> None:
        super().__init__(id=id)
        self._dispatch = dispatch
        self.display = False

    def set_actions(self, actions: list[Action], collapsed: Optional[bool] = None,
                    width: Optional[int] = None, collapse_below: int = COLLAPSE_AT) -> None:
        """Replace what the bar shows. An empty list hides the whole bar (no row selected, or the
        row offers nothing) -- disabled buttons still show (S-UI 4.4: "the disabled state is the
        explanation"), only an EMPTY bar is hidden.

        `width` is the pane's content width; the bar picks the widest form that fits (see the
        module docstring). `collapsed=True/False` overrides that decision when a caller has a
        reason of its own."""
        acts = list(actions)
        form = choose_form(acts, width, collapse_below)
        if collapsed is None:
            collapsed = form is None
        self.form = form or "full"
        self.collapsed = bool(collapsed)
        self.actions = tuple(acts)
        self.display = bool(acts)

    def compose(self) -> ComposeResult:
        acts = self.actions
        if not acts:
            return
        if self.collapsed:
            yield Button(COLLAPSE_LABEL, id=COLLAPSE_ID, variant="primary")
            return
        for a in acts:
            btn = Button(label_for(a, self.form), id=f"act-{a.action_name}", variant=a.variant,
                         disabled=not a.enabled)
            if a.tooltip:
                btn.tooltip = a.tooltip
            yield btn

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id == COLLAPSE_ID:
            self._dispatch(COLLAPSE_ID)
        else:
            self._dispatch(button_id[len("act-"):])
