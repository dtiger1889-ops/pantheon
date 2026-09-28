"""The one modal shape for every confirm and picker.

Two screens only, everywhere in the deck: `Confirm` for a yes/no question, `Pick` for choosing
one of a short list. Both are centred boxes, at most 60 columns wide and 12 rows tall, Escape
always cancels, and both carry the same
accelerator keys the user already uses (`y`/`n` for a confirm; a short letter or digit per option
for a pick) so nothing he learned stops working now that it opens in a box instead of the footer.

Nothing here fires its own command. Per S-UI section 6.4: a picker never fires its command while
the modal is open -- the caller's `push_screen(..., callback)` callback does that, after dismiss,
and only then does the pane's status line report what happened.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import events as tevents
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label, OptionList, Static, TextArea, Tree
from textual.widgets.option_list import Option

from ..dispatch import projects as projects_mod
from ..notes import complete as complete_mod

DESK_AT = 80          # the S-UI "P" width; below this the modal keeps its phone size
PHONE_WIDTH = 50
DESK_WIDTH = 76
PHONE_MAX_HEIGHT = 12
DESK_MAX_HEIGHT = 20


def _size_box(screen: ModalScreen) -> None:
    """Widen the modal at the desk: 50 columns / 12 rows on the phone, 76 / 20 at
    80 screen columns and up, set from `self.app.size.width` on mount -- per the note on
    `Confirm`'s CSS, the box stays a literal fixed number either way, never `auto` or `%`."""
    box = screen.query_one("#box", Vertical)
    wide = screen.app.size.width >= DESK_AT
    box.styles.width = DESK_WIDTH if wide else PHONE_WIDTH
    box.styles.max_height = DESK_MAX_HEIGHT if wide else PHONE_MAX_HEIGHT


@dataclass(frozen=True)
class PickOption:
    """One line of a `Pick` list: `key  label  detail` (S-UI section 6.3)."""

    key: str
    value: str
    label: str
    detail: str = ""
    disabled: bool = False


def numbered(pairs: list[tuple[str, str]], details: Optional[dict[str, str]] = None) -> list[PickOption]:
    """Build `PickOption`s keyed `1`..`9`, then `a`, `b`, ... when nothing more mnemonic makes
    sense, e.g. the model alias list -- there is no obvious single letter for `opusplan` vs
    `opus[1m]`. Letters continue the sequence because a two-character "key" like `10` can never
    arrive as one keypress."""
    details = details or {}

    def _key(i: int) -> str:
        return str(i + 1) if i < 9 else chr(ord("a") + i - 9)

    return [
        PickOption(_key(i), value, label, details.get(value, ""))
        for i, (value, label) in enumerate(pairs)
    ]


class Confirm(ModalScreen[Optional[bool]]):
    """A yes/no question. `Yes` is the LEFT button; `No` is `primary` and focused by default,
    because the safe answer should be the easy one to hit by accident. `Yes` only gets the red
    `error` variant when `danger=True` -- reserved for a confirm whose `Yes` is actually
    destructive (kill, hand-off), per design system R3 ("Three tiers only": Warning red is
    reserved for that tier, not spent on every confirm regardless of what it does). Every other
    confirm's `Yes` is `primary`, the same colour as `No`, so the red keeps its meaning
. Escape and the `n` key both cancel with
    `False`; only an explicit `Yes` (click or its accelerator, default `y`) returns `True`.
    Escape/close-without-choosing returns `None` so a caller can tell "said no" from "backed out"
    if it ever needs to."""

    BINDINGS = [("escape", "dismiss(None)", "cancel")]

    # The outer box is a FIXED width (never `auto`): an `auto`-width parent whose own children
    # are sized as a percentage of it is a circular measurement Textual resolves to zero, which
    # silently clips the buttons out of the click-hit-test region while still reporting a non-zero
    # `.region`. the CSS number below is the
    # phone default; `on_mount` widens it to a still-FIXED 76 at the desk, so the
    # no-`auto`-no-`%` rule above keeps holding at both sizes.
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical#box {
        width: 50; height: auto; max-height: 12;
        border: round $secondary; background: $panel; padding: 1 2;
    }
    Confirm #question { width: 100%; content-align: center middle; padding-bottom: 1; }
    Confirm #body { width: 100%; color: $text-muted; padding-bottom: 1; }
    Confirm #buttons { align: center middle; height: auto; width: 100%; }
    Confirm Button { margin: 0 1; }
    """

    def __init__(
        self,
        question: str,
        yes_label: str = "Yes (y)",
        no_label: str = "No (n)",
        body: str = "",
        accelerators: Optional[dict[str, Optional[bool]]] = None,
        name: Optional[str] = None,
        danger: bool = False,
    ) -> None:
        super().__init__(name=name)
        self.question = question
        self.yes_label = yes_label
        self.no_label = no_label
        self.body = body
        self.danger = danger
        # y/n are always there so nothing the user already knows stops working (S-UI section 6.1).
        self._accelerators: dict[str, Optional[bool]] = accelerators or {"y": True, "n": False}

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.question, id="question")
            if self.body:
                yield Static(self.body, id="body")
            with Horizontal(id="buttons"):
                yield Button(self.yes_label, id="yes", variant="error" if self.danger else "primary")
                yield Button(self.no_label, id="no", variant="primary")

    def on_mount(self) -> None:
        _size_box(self)
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "yes")

    def on_key(self, event: tevents.Key) -> None:
        if event.key in self._accelerators:
            event.stop()
            self.dismiss(self._accelerators[event.key])


class Pick(ModalScreen[Optional[str]]):
    """Choose one of a short list. Lines read `key  label  detail` in an `OptionList`; Enter or a
    click picks the highlighted line, and every option's own key also works directly, the same way
    the inline `c`/`x`/`i`/`l` prompt used to (S-UI section 6.3). The current value, if any, is
    marked with `●`. Escape (or a disabled/empty pick) returns `None` -- nothing was chosen."""

    BINDINGS = [("escape", "dismiss(None)", "cancel")]

    # See the note on `Confirm`'s CSS: the box is a fixed width, never `auto`, so the `100%`
    # widths inside it (the title, the OptionList) resolve to a real number instead of zero.
    # the CSS number is the phone default; `on_mount` widens it at the desk.
    DEFAULT_CSS = """
    Pick { align: center middle; }
    Pick > Vertical#box {
        width: 50; height: auto; max-height: 12;
        border: round $secondary; background: $panel; padding: 1 2;
    }
    Pick #title { width: 100%; padding-bottom: 1; }
    Pick OptionList { width: 100%; height: auto; max-height: 8; }
    """

    def __init__(
        self,
        title: str,
        options: list[PickOption],
        current: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(name=name)
        self.title_text = title
        self.options = list(options)
        self.current = current
        self._by_key = {o.key: o for o in self.options if o.key}

    def _line(self, o: PickOption) -> str:
        mark = "●" if o.value == self.current else " "
        bits = [mark, o.key, o.label]
        if o.detail:
            bits.append(o.detail)
        line = "  ".join(b for b in bits if b != "")
        # A line longer than the box clips into unreadable fragments; cut it to the box's inner width with a visible
        # ellipsis instead. Same fixed-width numbers _size_box uses; 6 = border + padding.
        try:
            wide = self.app.size.width >= DESK_AT
        except Exception:  # unit tests build lines without a running app; fail open to desk width
            wide = True
        avail = (DESK_WIDTH if wide else PHONE_WIDTH) - 6
        if len(line) > avail:
            line = line[: avail - 1] + "…"
        return line

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.title_text, id="title")
            option_list = OptionList(id="options")
            yield option_list

    def on_mount(self) -> None:
        _size_box(self)
        option_list = self.query_one("#options", OptionList)
        for o in self.options:
            option_list.add_option(Option(self._line(o), id=o.value, disabled=o.disabled))
        # An `OptionList` starts with nothing highlighted, so Enter would do nothing until the user
        # pressed an arrow key first; land on the current value (or the first option) instead.
        start = next((i for i, o in enumerate(self.options) if o.value == self.current), 0)
        if self.options:
            option_list.highlighted = start
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_key(self, event: tevents.Key) -> None:
        opt = self._by_key.get(event.key)
        if opt is not None and not opt.disabled:
            event.stop()
            self.dismiss(opt.value)


class LaunchOptions(ModalScreen[Optional[dict]]):
    """After who works a new session, only for Claude:
    three short columns -- model, effort, permission mode -- each an `OptionList` starting on
    `default`. Tab/Shift+Tab move between columns; Enter in a column moves to the next one (the
    last one confirms, same as the `Start` button); Escape cancels the whole screen.

    Returns a dict `{"model": ..., "effort": ..., "mode": ...}` -- `None` for a column left on
    `default`. `models`/`efforts` are plain value lists (`efforts` gets `default` prepended
    here since `EFFORT_LEVELS` has no such entry of its own; `models` doesn't need it --
    `MODEL_ALIASES` already leads with `default`); `modes` is `session_ctl.MODE_CYCLE`-shaped,
    `[(key, value), ...]` (`supervisor/pane.MODE_OPTIONS`), with a synthetic leading `default`
    meaning "leave it" -- distinct in intent from the real `default` permission mode already inside
    that list, though picking either produces the same no-op on a freshly launched session."""

    BINDINGS = [
        ("escape", "dismiss(None)", "cancel"),
        ("tab", "next_column", "next"),
        ("shift+tab", "prev_column", "prev"),
    ]

    DEFAULT_CSS = """
    LaunchOptions { align: center middle; }
    LaunchOptions > Vertical#box {
        width: 76; height: auto; max-height: 20;
        border: round $primary; border-title-color: $primary; border-title-style: bold;
        background: $panel; padding: 1 2;
    }
    LaunchOptions #columns { height: auto; width: 100%; }
    LaunchOptions .column { width: 1fr; height: auto; margin: 0 1 0 0; }
    LaunchOptions .column-title { width: 100%; color: $primary; text-style: bold; padding-bottom: 1; }
    LaunchOptions OptionList { height: 8; }
    LaunchOptions #launch-buttons { height: auto; width: 100%; margin-top: 1; align: center middle; }
    LaunchOptions #launch-hint { width: 100%; color: $text-muted; margin-top: 1; }
    """

    _COLUMN_IDS = ("col-model", "col-effort", "col-mode")

    def __init__(self, models: list[str], efforts: list[str], modes: list[tuple[str, str]],
                 name: Optional[str] = None, mode_label: str = "Permission mode") -> None:
        super().__init__(name=name)
        self.mode_label = mode_label   # "Permission mode" for Claude, "Sandbox" for Codex
        self.models = list(models)                                   # already leads with "default"
        self.efforts = ["default"] + list(efforts)
        self.modes = ["default"] + [value for _key, value in modes]

    @staticmethod
    def _options(values: list[str]) -> list[Option]:
        # The mode column's "default"
        # can collide, character for character, with the real `default` permission mode already
        # inside `modes` -- `OptionList` requires unique ids, so the id is the row's own index,
        # never the value text; `_confirm` reads the value back out of the source list by index.
        return [Option(v, id=str(i)) for i, v in enumerate(values)]

    def compose(self) -> ComposeResult:
        with Vertical(id="box") as box:
            box.border_title = "launch options"
            with Horizontal(id="columns"):
                with Vertical(classes="column"):
                    yield Static("Model", classes="column-title")
                    yield OptionList(*self._options(self.models), id="col-model")
                with Vertical(classes="column"):
                    yield Static("Effort", classes="column-title")
                    yield OptionList(*self._options(self.efforts), id="col-effort")
                with Vertical(classes="column"):
                    yield Static(self.mode_label, classes="column-title")
                    yield OptionList(*self._options(self.modes), id="col-mode")
            with Horizontal(id="launch-buttons"):
                yield Button("Start (Enter)", id="start", variant="primary")
            yield Static("Tab moves columns · Enter/Start confirms · Esc cancels", id="launch-hint")

    def on_mount(self) -> None:
        _size_box(self)
        for cid in self._COLUMN_IDS:
            self.query_one(f"#{cid}", OptionList).highlighted = 0
        self.query_one(f"#{self._COLUMN_IDS[0]}", OptionList).focus()

    def _move_column(self, delta: int) -> None:
        ids = self._COLUMN_IDS
        focused_id = self.focused.id if self.focused is not None else None
        try:
            idx = ids.index(focused_id)
        except ValueError:
            idx = 0
        self.query_one(f"#{ids[(idx + delta) % len(ids)]}", OptionList).focus()

    def action_next_column(self) -> None:
        self._move_column(1)

    def action_prev_column(self) -> None:
        self._move_column(-1)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        ids = self._COLUMN_IDS
        idx = ids.index(event.option_list.id)
        if idx + 1 < len(ids):
            self.query_one(f"#{ids[idx + 1]}", OptionList).focus()
        else:
            self._confirm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "start":
            self._confirm()

    def _confirm(self) -> None:
        result: dict[str, Optional[str]] = {}
        for key, cid, values in zip(("model", "effort", "mode"), self._COLUMN_IDS,
                                    (self.models, self.efforts, self.modes)):
            options = self.query_one(f"#{cid}", OptionList)
            index = options.highlighted if options.highlighted is not None else 0
            index = max(0, min(index, len(values) - 1))
            value = values[index]
            result[key] = None if value == "default" else value
        self.dismiss(result)


def _picker_state_path(cfg) -> Path:
    return Path(cfg.state_dir) / "ui" / "picker.json"


def _load_picker_state(cfg) -> tuple[str, str]:
    """The remembered view/sort (`state/ui/picker.json`); fails open to the MVP defaults
    (list, recent) on any read problem -- a missing/corrupt file is not an error here."""
    try:
        data = json.loads(_picker_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "list", "recent"
    view = data.get("view") if data.get("view") in ("list", "tree") else "list"
    sort = data.get("sort") if data.get("sort") in ("recent", "name") else "recent"
    return view, sort


def _save_picker_state(cfg, view: str, sort: str) -> None:
    try:
        path = _picker_state_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"view": view, "sort": sort}), encoding="utf-8")
    except OSError:
        pass   # fail-open: remembering the view is a nicety, never a reason to block the picker


_MAX_TREE_DEPTH = 3   # how many folder levels deep the tree walks from `projects_root`. Depth 1
                      # is a root-level entry; depth 3 is three folders below `projects_root`.
_UNICODE_TREE_GLYPHS = {"leaf": "·", "recent": "●", "expand": "▸ ", "expand_open": "▾ "}
_ASCII_TREE_GLYPHS = {"leaf": ".", "recent": "*", "expand": "> ", "expand_open": "v "}
# Nerd Font icons when `pantheon.toml` `[appearance] icons = true` -- `getattr` still guards a `cfg` built before that field
# existed.
_NERD_TREE_GLYPHS = {"leaf": "\uf115", "recent": "\uf111", "expand": "\uf0da ", "expand_open": "\uf0d7 "}

# The Tree/List/Recent/A-Z toggle row's full labels (id -> (short, full)): at the phone width
# (65 columns) the four full labels plus their "(Ctrl+X)" suffixes don't fit the box's content
# width, pushing the A-Z button's region past screen.size.width -- unclickable and, on a real
# 65-column phone terminal, clipped off-screen. Below `DESK_AT` the picker drops the suffix
# (regression: `test_picker_toggle_labels_fit_the_phone_width`); the mnemonic isn't lost, since
# `#picker-hint` already spells out every accelerator key at every width.
_PICKER_TOGGLE_LABELS = {
    "view-tree": ("Tree", "Tree (Ctrl+T)"),
    "view-list": ("List", "List (Ctrl+L)"),
    "sort-recent": ("Recent", "Recent (Ctrl+R)"),
    "sort-name": ("A-Z", "A-Z (Ctrl+A)"),
}


class ProjectPicker(ModalScreen[Optional[str]]):
    """The project picker behind "New session". A large framed screen (about 90% of the terminal at the desk, full screen under
    80 columns -- `DESK_AT`): a filter `Input`, a Tree/List view toggle, a Recent/A-Z sort toggle,
    an `Add folder...` button for a path outside `projects_root`, and the list or tree itself.

    LIST view is the original flat `OptionList`. TREE view is a `Textual` `Tree` rooted (invisibly) on every `Project` this
    picker was given, lazily expanded one level into each project's own marked subfolders (a
    nested repo like `Gazebo/pinecone_catalog`) the first time that node opens -- `_tree_children`
    decides what is worth showing there. Typing in the filter always forces List (a Tree has
    nothing sensible to filter down to); clearing it back out returns to whichever view was open
    before the first keystroke. The last view and sort persist in `state/ui/picker.json`
    (`_load_picker_state` / `_save_picker_state`), fail-open, so a picker built with no `cfg` behaves exactly as it always did: List, Recent, nothing written to disk.

    Enter, or a click on a line, picks a project node; a container node -- a folder that is not
    itself a project but holds one somewhere inside it -- only expands. Returns the project folder
    path, or `None` on Escape."""

    # `priority=True`: the filter `Input` keeps focus throughout, and `Input`
    # already binds several of these combinations for its own editing (`ctrl+a` is "go to start
    # of line" -- found while writing the Ctrl+A sort test), which would otherwise win every time
    # since a focused widget's own bindings are checked before the screen's. Same fix
    # `SupervisorPane` uses on `enter` against `DataTable`'s own binding.
    BINDINGS = [
        ("escape", "dismiss(None)", "cancel"),
        Binding("ctrl+r", "sort('recent')", "recent", priority=True),
        Binding("ctrl+a", "sort('name')", "A-Z", priority=True),
        Binding("ctrl+t", "set_view('tree')", "tree", priority=True),
        Binding("ctrl+l", "set_view('list')", "list", priority=True),
        Binding("ctrl+o", "add_folder", "add folder", priority=True),
        Binding("ctrl+e", "most_recent", "most recent", priority=True),
    ]

    DEFAULT_CSS = """
    ProjectPicker { align: center middle; }
    ProjectPicker > Vertical#box {
        width: 76; height: 24;
        border: round $primary; border-title-color: $primary; border-title-style: bold;
        background: $panel; padding: 1 2;
    }
    ProjectPicker #picker-top { height: auto; width: 100%; }
    ProjectPicker #filter { width: 1fr; }
    ProjectPicker #picker-controls, ProjectPicker #picker-controls-2 {
        height: auto; width: 100%; margin-top: 1;
    }
    ProjectPicker #picker-controls Button, ProjectPicker #picker-controls-2 Button {
        height: 1; min-width: 0; margin: 0 1 0 0; padding: 0 1; border: none !important;
        border-top: none !important; border-bottom: none !important;
        background: $surface-lighten-1; color: $foreground; text-style: none;
    }
    ProjectPicker #picker-controls Button.-primary { background: $primary-muted; color: $text-primary; text-style: bold; }
    ProjectPicker #projects { width: 100%; height: 1fr; margin-top: 1; }
    ProjectPicker #tree { width: 100%; height: 1fr; margin-top: 1; background: $panel; }
    ProjectPicker #picker-status { width: 100%; color: $warning; height: auto; }
    ProjectPicker #picker-hint { width: 100%; color: $text-muted; margin-top: 1; height: auto; }
    """

    def __init__(self, projects, cfg=None, sort: Optional[str] = None, view: Optional[str] = None,
                 name: Optional[str] = None, root: Optional[str] = None,
                 workspace: Optional[projects_mod.Project] = None) -> None:
        super().__init__(name=name)
        self.projects = list(projects)
        self.root = (root or "").replace(chr(92), "/").rstrip("/")   # folders under it show no path
        self.workspace = workspace   # `projects_root` itself, always shown first (Ctrl+O workaround)
        self.cfg = cfg
        loaded_view, loaded_sort = _load_picker_state(cfg) if cfg is not None else ("list", "recent")
        self.sort_by = sort or loaded_sort
        self.view = view or loaded_view
        self._shown: list = []
        self._pre_filter_view: Optional[str] = None   # the view a filter keystroke auto-left
        self.status_text = ""   # the Add-folder status line, mirrored here for tests to read
        # Bug found running this brief: switching Tree<->List, or re-sorting, always snapped the
        # highlight back to row 0 -- losing your place on the exact project you were just looking
        # at (this is a chunk of what read as "buggy" to a cold user toggling those controls).
        # `_active_path` is the project path currently highlighted, in whichever view is on
        # screen; `_fill`/`_rebuild_tree` re-land on it when it is still visible in the rebuilt
        # list/tree, and fall back to the first row exactly like before when it is not.
        self._active_path: Optional[str] = None

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Vertical(id="box") as box:
            box.border_title = "New session · pick a project"
            with Horizontal(id="picker-top"):
                yield Input(placeholder="type to filter projects", id="filter")
            with Horizontal(id="picker-controls"):
                # Labels start full; `_size_box` (called from `on_mount`) drops the "(Ctrl+X)"
                # suffix below `DESK_AT` before the first paint if the app is already narrow.
                yield Button(_PICKER_TOGGLE_LABELS["view-tree"][1], id="view-tree",
                             variant="primary" if self.view == "tree" else "default")
                yield Button(_PICKER_TOGGLE_LABELS["view-list"][1], id="view-list",
                             variant="primary" if self.view == "list" else "default")
                yield Button(_PICKER_TOGGLE_LABELS["sort-recent"][1], id="sort-recent",
                             variant="primary" if self.sort_by == "recent" else "default")
                yield Button(_PICKER_TOGGLE_LABELS["sort-name"][1], id="sort-name",
                             variant="primary" if self.sort_by == "name" else "default")
            with Horizontal(id="picker-controls-2"):
                yield Button("Add folder… (Ctrl+O)", id="add-folder")
                yield Button("Most recent (Ctrl+E)", id="most-recent")
            yield OptionList(id="projects")
            yield Tree("projects", id="tree")
            yield Static("", id="picker-status")
            yield Static(
                "Enter picks · Esc cancels · Ctrl+T tree · Ctrl+L list · "
                "Ctrl+R recent · Ctrl+A A-Z · Ctrl+O add folder · Ctrl+E most recent",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        self._size_box()
        tree = self.query_one("#tree", Tree)
        glyphs = self._glyphs()
        tree.show_root = False
        tree.auto_expand = False   # Enter picks a project node instead of also toggling it
        tree.ICON_NODE = glyphs["expand"]
        tree.ICON_NODE_EXPANDED = glyphs["expand_open"]
        self._fill()
        self._apply_view(build_tree=self.view == "tree", remember=False)
        self.query_one("#filter", Input).focus()

    def on_resize(self, event: tevents.Resize) -> None:
        self._size_box()

    def _size_box(self) -> None:
        """About 90% of the terminal at the desk (`DESK_AT` and up), the whole screen under it
        -- a literal number either way, never `auto`/`%` (see the note on
        `Confirm`'s CSS for why a percentage inside this parent is unsafe)."""
        box = self.query_one("#box", Vertical)
        width = self.app.size.width or 80
        height = self.app.size.height or 24
        if width < DESK_AT:
            box.styles.width = width
            box.styles.height = height
        else:
            box.styles.width = max(60, min(width - 4, int(width * 0.9)))
            box.styles.height = max(20, min(height - 2, int(height * 0.9)))
        short = width < DESK_AT
        for bid, (short_label, full_label) in _PICKER_TOGGLE_LABELS.items():
            self.query_one(f"#{bid}", Button).label = short_label if short else full_label

    # ------------------------------------------------------------------ glyphs

    def _glyphs(self) -> dict[str, str]:
        mode, icons = "unicode", False
        if self.cfg is not None:
            appearance = self.cfg.appearance_settings()
            mode = appearance.glyphs
            icons = bool(getattr(appearance, "icons", False))
        if icons:
            return _NERD_TREE_GLYPHS
        if (mode or "").lower() == "ascii":
            return _ASCII_TREE_GLYPHS
        return _UNICODE_TREE_GLYPHS

    # ------------------------------------------------------------------ list view

    def _fill(self) -> None:
        text = self.query_one("#filter", Input).value
        shown = projects_mod.sort_projects(projects_mod.filter_projects(self.projects, text), self.sort_by)
        if self.workspace is not None and projects_mod.filter_projects([self.workspace], text):
            shown = [self.workspace] + shown   # fixed first, regardless of sort
        self._shown = shown
        options = self.query_one("#projects", OptionList)
        options.clear_options()
        for p in shown:
            line = Text()
            line.append(f"{p.name:<28.28}", style="bold")
            line.append(f"  {p.last_used_text:>8}", style="dim")
            if self.root and p.path.lower().startswith(self.root.lower() + "/"):
                pass   # inside the projects root: the name says it all
            else:
                line.append(f"  {p.path}", style="dim")
            options.add_option(Option(line, id=p.path))
        if shown:
            options.highlighted = self._restart_index(shown)
        if not shown:
            options.add_option(Option(Text("no project matches that", style="dim"), disabled=True))

    def _restart_index(self, shown: list) -> int:
        """Where to land the highlight after a rebuild: the row still showing `_active_path`,
        or row 0 when it is gone (filtered out, or never set) -- the selection-stability fix."""
        if self._active_path:
            for i, p in enumerate(shown):
                if p.path.lower() == self._active_path.lower():
                    return i
        return 0

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        text = event.value
        if text and self.view != "list":
            self._pre_filter_view = self.view
            self._apply_view(new_view="list", remember=False)
        elif not text and self._pre_filter_view is not None:
            previous = self._pre_filter_view
            self._pre_filter_view = None
            self._apply_view(new_view=previous, remember=False)
        self._fill()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._activate_current()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self.dismiss(event.option.id)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        # Tracks the highlight as it moves (arrow keys, mouse hover) so a later filter/sort/view
        # change can try to land back on the same project instead of always snapping to row 0.
        if event.option.id:
            self._active_path = event.option.id

    def _activate_current(self) -> None:
        """Enter while the filter box has focus -- picks whatever
        is highlighted in whichever view is on screen."""
        if self.view == "tree":
            node = self.query_one("#tree", Tree).cursor_node
            if node is not None:
                self._activate_tree_node(node)
            return
        if self._shown:
            options = self.query_one("#projects", OptionList)
            index = options.highlighted if options.highlighted is not None else 0
            index = max(0, min(index, len(self._shown) - 1))
            self.dismiss(self._shown[index].path)

    # ------------------------------------------------------------------ tree view

    def _tree_children(self, folder: Path, depth: int = 0) -> list[tuple[Path, bool]]:
        """Immediate subfolders of `folder` worth showing under it: every marked project, plus
        every unmarked folder that itself holds a marked project somewhere within `_MAX_TREE_DEPTH`
        levels further down -- so a container like `Apps/`, or a nested repo two or three folders
        deep, stays reachable without dumping every unrelated folder into the tree. `depth` is
        `folder`'s own distance from `projects_root` (0 there); a folder without a marker anywhere
        inside the remaining budget is left out entirely -- "collapses" -- rather than shown as a
        dead end, per the build brief."""
        try:
            children = sorted(p for p in folder.iterdir()
                             if p.is_dir() and not p.name.startswith((".", "_")))
        except OSError:
            return []
        out: list[tuple[Path, bool]] = []
        for child in children:
            if projects_mod._is_project(child):
                out.append((child, True))
                continue
            if depth < _MAX_TREE_DEPTH and self._holds_marked_project(
                child, _MAX_TREE_DEPTH - depth - 1
            ):
                out.append((child, False))
        return out

    def _holds_marked_project(self, folder: Path, remaining: int) -> bool:
        """Does `folder` hold a CLAUDE.md/git-marked project at or within `remaining` levels
        beneath it? The bounded probe behind `_tree_children`'s container test -- `remaining`
        counts down to 0 so an unrelated folder tree costs a handful of `os.scandir` calls, never
        an unbounded walk."""
        try:
            children = list(folder.iterdir())
        except OSError:
            return False
        for child in children:
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            if projects_mod._is_project(child):
                return True
            if remaining > 0 and self._holds_marked_project(child, remaining - 1):
                return True
        return False

    def _ad_hoc_project(self, folder: Path) -> projects_mod.Project:
        """A `Project` for a nested tree node that was never in `self.projects` -- reuses the real one when a nested
        folder happens to match (an agent was started right there), else `last_used=None`."""
        path = projects_mod._norm(str(folder))
        if self.workspace is not None and path.lower() == self.workspace.path.lower():
            return self.workspace
        known = next((p for p in self.projects if p.path.lower() == path.lower()), None)
        if known is not None:
            return known
        try:
            mtime = folder.stat().st_mtime
        except OSError:
            mtime = 0.0
        return projects_mod.Project(folder.name, path, None, mtime)

    def _tree_label(self, folder: Path, is_project: bool, allow_expand: bool) -> Text:
        glyphs = self._glyphs()
        text = Text()
        if not is_project:
            text.append(folder.name, style="dim")   # a container: no age, no marker, just a name
            return text
        p = self._ad_hoc_project(folder)
        recent = p.last_used is not None and (datetime.now(timezone.utc) - p.last_used).total_seconds() < 3600
        if not allow_expand:
            # A leaf gets no arrow from the Tree itself, so the leading glyph fills that gap
            #.
            text.append(f"{glyphs['recent'] if recent else glyphs['leaf']} ")
        text.append(p.name, style="bold")
        text.append(f"  {p.last_used_text:>8}", style="dim")
        markers = [m.lstrip(".") for m in projects_mod.MARKERS if (folder / m).exists()]
        if markers:
            text.append(f"  {'/'.join(markers)}", style="dim")
        return text

    def _add_tree_node(self, parent, folder: Path, is_project: bool, depth: int = 1) -> None:
        """`depth` is this node's own distance from `projects_root` (root-level entries are 1) --
        threaded through so `_tree_children`'s bounded container scan (`_MAX_TREE_DEPTH`) and the
        expand-further decision below share one budget instead of each expansion getting a fresh
        one."""
        below_max = depth < _MAX_TREE_DEPTH
        nested = self._tree_children(folder, depth) if is_project else True   # containers always
                                                                                # expand (already
                                                                                # confirmed to hold
                                                                                # a project)
        # Bug found running this brief: `below_max` gated a CONTAINER's own
        # expand-ability too, using its absolute distance from `projects_root` -- but
        # `_tree_children` already used that same budget to decide the container belongs in the
        # tree at all (a marked project is added unconditionally, with no depth limit of its own,
        # the moment it turns up as someone's direct child). A container could therefore be
        # included at exactly `depth == _MAX_TREE_DEPTH` -- already confirmed to hold a project
        # one level further down -- and then be added as a dead leaf (`allow_expand=False`) that
        # could never actually be opened to reach it. Only a PROJECT node's own further nesting
        # (a repo inside a repo) is meant to stop at the depth budget; a container's expand-ability
        # was never supposed to be re-gated the same way.
        allow_expand = bool(nested) and (below_max if is_project else True)
        label = self._tree_label(folder, is_project, allow_expand)
        data = {"kind": "project" if is_project else "container",
                "path": projects_mod._norm(str(folder)), "populated": False, "depth": depth}
        if allow_expand:
            parent.add(label, data=data, allow_expand=True)
        else:
            parent.add_leaf(label, data=data)

    def _root_entries(self) -> list[tuple[Path, bool]]:
        """The top-level tree nodes: every marked-or-holds-a-marked-child folder directly under
        `projects_root` (via `_tree_children`, so an unmarked container like `Apps/` still shows
        when it holds a project), plus every project the event log has seen used somewhere else
       ."""
        entries: list[tuple[Path, bool]] = []
        seen = set()
        root_path = Path(self.root) if self.root else None
        if root_path is not None and root_path.is_dir():
            for child, is_proj in self._tree_children(root_path):
                entries.append((child, is_proj))
                seen.add(projects_mod._norm(str(child)).lower())
        for p in self.projects:
            if p.path.lower() not in seen:
                entries.append((Path(p.path), True))
                seen.add(p.path.lower())
        return entries

    def _sort_root_entries(self, entries: list[tuple[Path, bool]]) -> list[tuple[Path, bool]]:
        """Same ordering `dispatch.projects.sort_projects` uses for the list -- a container (no
        "used" concept of its own) sorts as never-used, same tier as an unused project."""
        lookup = {p.path.lower(): p for p in self.projects}

        def last_used(folder: Path, is_project: bool):
            if not is_project:
                return None
            proj = lookup.get(projects_mod._norm(str(folder)).lower())
            return proj.last_used if proj else None

        def mtime(folder: Path) -> float:
            try:
                return folder.stat().st_mtime
            except OSError:
                return 0.0

        if self.sort_by == "name":
            return sorted(entries, key=lambda e: e[0].name.lower())
        far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return sorted(
            entries,
            key=lambda e: (
                last_used(e[0], e[1]) is None,
                -(last_used(e[0], e[1]) or far_past).timestamp(),
                -mtime(e[0]),
                e[0].name.lower(),
            ),
        )

    def _rebuild_tree(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.root.remove_children()
        entries = self._sort_root_entries(self._root_entries())
        if self.workspace is not None:
            entries = [(f, p) for f, p in entries if f.as_posix().lower() != self.workspace.path.lower()]
            entries = [(Path(self.workspace.path), True)] + entries   # fixed first
        for folder, is_proj in entries:
            self._add_tree_node(tree.root, folder, is_proj)
        tree.root.expand()
        if tree.root.children:
            tree.cursor_line = self._restart_line(tree)

    def _restart_line(self, tree: Tree) -> int:
        """Same idea as `_restart_index`, for the tree's root-level rows (the only ones a
        rebuild can see -- a nested node that was expanded before a rebuild collapses again, same
        as it always did; there is nothing to restore it to)."""
        if self._active_path:
            for i, node in enumerate(tree.root.children):
                if (node.data or {}).get("path", "").lower() == self._active_path.lower():
                    return i
        return 0

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data or {}
        if data.get("kind") == "project":
            self._active_path = data.get("path")

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        event.stop()
        node = event.node
        data = node.data or {}
        if not data or data.get("populated"):
            return
        data["populated"] = True
        folder = Path(data.get("path", ""))
        depth = data.get("depth", 1)
        for child_path, is_proj in self._tree_children(folder, depth):
            self._add_tree_node(node, child_path, is_proj, depth + 1)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        event.stop()
        self._activate_tree_node(event.node)

    def _activate_tree_node(self, node) -> None:
        data = node.data or {}
        kind = data.get("kind")
        if kind == "project":
            self.dismiss(data.get("path"))
        elif kind == "container":
            node.toggle()

    # ------------------------------------------------------------------ view / sort toggles

    def _apply_view(self, new_view: Optional[str] = None, build_tree: bool = False,
                    remember: bool = True) -> None:
        if new_view is not None:
            self.view = new_view
        self.query_one("#view-tree", Button).variant = "primary" if self.view == "tree" else "default"
        self.query_one("#view-list", Button).variant = "primary" if self.view == "list" else "default"
        self.query_one("#projects", OptionList).display = self.view == "list"
        self.query_one("#tree", Tree).display = self.view == "tree"
        if self.view == "tree" and (new_view is not None or build_tree):
            self._rebuild_tree()
        if remember and self.cfg is not None:
            _save_picker_state(self.cfg, self.view, self.sort_by)

    def action_set_view(self, view: str) -> None:
        if view not in ("tree", "list"):
            return
        self._pre_filter_view = None
        if view == "tree":
            filt = self.query_one("#filter", Input)
            if filt.value:
                filt.value = ""   # a tree has nothing to filter down to
        self._apply_view(new_view=view)

    def action_sort(self, by: str) -> None:
        self.sort_by = by
        self.query_one("#sort-recent", Button).variant = "primary" if by == "recent" else "default"
        self.query_one("#sort-name", Button).variant = "primary" if by == "name" else "default"
        self._fill()
        if self.view == "tree":
            self._rebuild_tree()
        if self.cfg is not None:
            _save_picker_state(self.cfg, self.view, self.sort_by)

    # ------------------------------------------------------------------ add folder (Ctrl+O)

    def action_add_folder(self) -> None:
        """A folder outside `projects_root` -- browse for it instead of typing the path from
        memory. Starts at the projects
        root's own parent when there is one,
        else his home folder; `FolderBrowser` keeps a typed-path box one keystroke away (Ctrl+G)
        for a location browsing can't conveniently reach at all (a UNC share, a drive letter with
        nothing under this start point)."""
        start = str(Path(self.root).parent) if self.root and Path(self.root).is_dir() else str(Path.home())
        self.app.push_screen(FolderBrowser(start), self._add_folder_given)

    def _add_folder_given(self, path: Optional[str]) -> None:
        if not path:
            return
        normalized = path.strip().replace(chr(92), "/").rstrip("/")
        if not normalized or not Path(normalized).is_dir():
            self._set_status(f"that folder is not there: {path.strip()}")
            return
        self.dismiss(normalized)

    def _set_status(self, text: str) -> None:
        self.status_text = text   # a plain attribute a test can read without reaching into Static
        self.query_one("#picker-status", Static).update(text)

    # ------------------------------------------------------------------ most recent (Ctrl+E)

    def action_most_recent(self) -> None:
        """A one-key/one-click shortcut to whichever project (including the workspace root) was
        used most recently, regardless of what the filter, sort or view currently show -- D16
        "quick actions" (resume-recent, in effect: jump straight back to what you were just in,
        skipping the navigate-and-Enter dance). Cheap because `sort_projects` already knows how to
        rank by recency; there is nothing here to disable a nothing-used-yet state can't already
        answer with its own message."""
        pool = list(self.projects)
        if self.workspace is not None:
            pool.append(self.workspace)
        used = [p for p in pool if p.last_used is not None]
        if not used:
            self._set_status("nothing used yet")
            return
        self.dismiss(projects_mod.sort_projects(used, "recent")[0].path)

    # ------------------------------------------------------------------ buttons / keys

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id
        if bid == "sort-recent":
            self.action_sort("recent")
        elif bid == "sort-name":
            self.action_sort("name")
        elif bid == "view-tree":
            self.action_set_view("tree")
        elif bid == "view-list":
            self.action_set_view("list")
        elif bid == "add-folder":
            self.action_add_folder()
        elif bid == "most-recent":
            self.action_most_recent()

    def on_key(self, event: tevents.Key) -> None:
        # Arrows move the highlight in either view while the filter box keeps the focus, so
        # typing never stops.
        if event.key not in ("down", "up"):
            return
        if self.view == "tree":
            tree = self.query_one("#tree", Tree)
            event.stop()
            tree.action_cursor_down() if event.key == "down" else tree.action_cursor_up()
        elif self._shown:
            options = self.query_one("#projects", OptionList)
            event.stop()
            options.action_cursor_down() if event.key == "down" else options.action_cursor_up()


def _tree_path(path: str) -> str:
    """`C:/x` -> `/c/x` under MSYS2 Python. `DirectoryTree` calls `Path.resolve` before listing,
    and to a posix Python `C:/Users/...` is a RELATIVE path, so it resolved under the working
    folder, listing failed silently and Add folder showed an empty box."""
    m = re.match(r"^([A-Za-z]):[/\\]?(.*)$", path)
    if os.name != "posix" or not m:
        return path
    return f"/{m.group(1).lower()}/" + m.group(2).replace(chr(92), "/")


def _drive_path(path: str) -> str:
    """The reverse: `/c/x` -> `C:/x`, the form every other project path in Pantheon uses."""
    m = re.match(r"^/([A-Za-z])(/.*)?$", path)
    if os.name != "posix" or not m:
        return path
    return f"{m.group(1).upper()}:" + (m.group(2) or "/")


class _DirsOnly(DirectoryTree):
    """Folders only -- Add-folder is picking a destination folder, not a file to open, and a
    normal `DirectoryTree` full of every file in a project would bury the folders the user actually
    wants under everything else in it."""

    def filter_paths(self, paths):
        return [p for p in paths if p.is_dir() and not p.name.startswith(".")]


class FolderBrowser(ModalScreen[Optional[str]]):
    """A navigable directory tree for "Add folder", so pointing at a project outside `projects_root` doesn't mean typing its path from
    memory. Built on Textual's own `DirectoryTree` -- it already loads one level lazily as you
    expand, the same shape as `ProjectPicker`'s own project tree, so there is no bounded-depth walk
    to hand-roll here the way that one needed (a live filesystem browse never scans ahead of where
    you have actually clicked).

    A click on a folder opens or closes it (so the mouse can drill down); Enter or the `Choose this
    folder` button picks the highlighted one. Until 2026-09-27 a click picked the folder outright,
    so nothing below the first level was reachable by mouse. `Ctrl+G` (or its button) still opens
    the original typed-path box for anywhere the tree can't conveniently reach from its start point
    (a UNC share, another drive). Escape cancels the whole screen, same as everywhere else."""

    BINDINGS = [
        ("escape", "dismiss(None)", "cancel"),
        Binding("enter", "choose", "choose", priority=True),
        Binding("ctrl+g", "type_path", "type a path", priority=True),
    ]

    DEFAULT_CSS = """
    FolderBrowser { align: center middle; }
    FolderBrowser > Vertical#box {
        width: 76; height: 24;
        border: round $primary; border-title-color: $primary; border-title-style: bold;
        background: $panel; padding: 1 2;
    }
    FolderBrowser #browser-tree { width: 100%; height: 1fr; background: $panel; }
    FolderBrowser #browser-buttons { height: auto; width: 100%; margin-top: 1; }
    FolderBrowser #browser-buttons Button { margin: 0 1 0 0; }
    FolderBrowser #browser-status { width: 100%; color: $warning; height: auto; }
    FolderBrowser #browser-hint { width: 100%; color: $text-muted; margin-top: 1; height: auto; }
    """

    def __init__(self, start: str, name: Optional[str] = None) -> None:
        super().__init__(name=name)
        self.start = _tree_path(start if start and Path(start).is_dir() else str(Path.home()))
        self.status_text = ""   # mirrored here for tests, same pattern as ProjectPicker

    def compose(self) -> ComposeResult:
        with Vertical(id="box") as box:
            box.border_title = "add a folder"
            yield _DirsOnly(self.start, id="browser-tree")
            with Horizontal(id="browser-buttons"):
                yield Button("Choose this folder (Enter)", id="choose", variant="primary")
                yield Button("Type a path… (Ctrl+G)", id="type-path")
            yield Static("", id="browser-status")
            yield Static(
                "Enter/click chooses the highlighted folder · Ctrl+G types a path instead · Esc cancels",
                id="browser-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#browser-tree", _DirsOnly).focus()

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        event.stop()   # a click: the tree already opened/closed the folder; choosing is Enter/button

    def action_choose(self) -> None:
        tree = self.query_one("#browser-tree", _DirsOnly)
        node = tree.cursor_node
        if node is None or node.data is None:
            self._set_status("nothing highlighted yet")
            return
        self._choose(node.data.path)

    def action_type_path(self) -> None:
        self.app.push_screen(
            TextPrompt("add a folder", placeholder="C:/path/to/folder", hint="Enter adds · Esc cancels"),
            self._typed_path_given,
        )

    def _typed_path_given(self, path: Optional[str]) -> None:
        if not path:
            return
        self._choose(path)

    def _choose(self, path) -> None:
        normalized = str(path).strip().replace(chr(92), "/").rstrip("/")
        if not normalized or not Path(normalized).is_dir():
            self._set_status(f"that folder is not there: {path}")
            return
        self.dismiss(_drive_path(normalized))

    def _set_status(self, text: str) -> None:
        self.status_text = text
        self.query_one("#browser-status", Static).update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "choose":
            self.action_choose()
        elif event.button.id == "type-path":
            self.action_type_path()


class TextPrompt(ModalScreen[Optional[str]]):
    """Free text, e.g. the first message for a new session -- a proper multi-line box, not a single-line `Input`. Enter sends the whole thing (empty string
    when left blank, same contract as before this pass, so a caller that only ever typed one line
    -- renaming a draft -- keeps working exactly as it did); `Shift+Enter`/`Ctrl+J` insert a literal
    newline instead of sending, for the rarer message that actually needs one; `Ctrl+Space` opens
    slash-command completion at the caret, reusing `notes/complete.py` (built reusable for exactly
    this) rather than a second copy of the same lookup. Escape cancels the whole screen.

    `TextArea` has no built-in placeholder (unlike `Input`), so a non-empty `placeholder` renders
    as a small muted line above the box instead of ghost text inside it -- visible the whole time
    you're typing, not just before the first keystroke, which is arguably the more useful version
    of the same hint anyway."""

    BINDINGS = [
        ("escape", "dismiss(None)", "cancel"),
        Binding("enter", "submit", "send", priority=True),
        Binding("shift+enter", "newline", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", "newline", show=False, priority=True),
        Binding("ctrl+space", "complete", "commands", show=False, priority=True),
    ]

    # The box had a literal `width: 76` here with no `_size_box` call (every sibling modal --
    # `Pick`, `ProjectPicker`, `Confirm` -- has both), so at a phone width the box was
    # wider than the screen and clipped off the right edge. `#box` here (like the other modals'
    # `#box`) is what `_size_box` widens at the desk and keeps at `PHONE_WIDTH` on the phone.
    DEFAULT_CSS = """
    TextPrompt { align: center middle; }
    TextPrompt > Vertical#box {
        width: 50; height: auto; max-height: 18;
        border: round $primary; border-title-color: $primary; border-title-style: bold;
        background: $panel; padding: 1 2;
    }
    TextPrompt #prompt-placeholder { width: 100%; color: $text-muted; padding-bottom: 1; }
    TextPrompt #prompt-text { width: 100%; height: 6; border: round $secondary; }
    TextPrompt #prompt-hint { width: 100%; color: $text-muted; margin-top: 1; }
    """

    def __init__(self, title: str, placeholder: str = "",
                 hint: str = "Enter sends · Shift+Enter newline · Esc cancels",
                 name: Optional[str] = None, claude_home: Optional[str] = None,
                 text: str = "") -> None:
        super().__init__(name=name)
        self.title_text = title
        self.placeholder = placeholder
        self.initial_text = text   # already in the box when it opens
        self.hint = hint
        self.claude_home = claude_home
        self._commands: Optional[list[complete_mod.Command]] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="box") as box:
            box.border_title = self.title_text
            if self.placeholder:
                yield Static(self.placeholder, id="prompt-placeholder")
            yield TextArea(self.initial_text, id="prompt-text", soft_wrap=True)
            yield Static(self.hint, id="prompt-hint")

    def on_mount(self) -> None:
        _size_box(self)
        self.query_one("#prompt-text", TextArea).focus()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#prompt-text", TextArea).text)

    def action_newline(self) -> None:
        self.query_one("#prompt-text", TextArea).insert("\n")

    # ------------------------------------------------------------------ slash completion

    def _loaded_commands(self) -> list[complete_mod.Command]:
        if self._commands is None:
            try:
                home = self.claude_home or str(Path.home() / ".claude")
                extra = complete_mod.default_skills_dirs(home)
                self._commands = complete_mod.list_commands(home, extra)
            except Exception:   # a bad commands/skills folder must never block typing a message
                self._commands = []
        return self._commands

    def action_complete(self) -> None:
        area = self.query_one("#prompt-text", TextArea)
        row, col = area.cursor_location
        line = area.document.get_line(row)
        token = complete_mod.token_at(line[:col]) or "/"
        matches = complete_mod.match(token, self._loaded_commands())
        if not matches:
            return
        options = numbered(
            [(c.name, c.name) for c in matches],
            {c.name: c.description for c in matches},
        )
        start = (row, col - len(token))
        self.app.push_screen(
            Pick(f"commands matching '{token}'", options),
            lambda chosen: self._completed(chosen, start, (row, col)),
        )

    def _completed(self, chosen: Optional[str], start: tuple, end: tuple) -> None:
        if chosen is None:
            return
        area = self.query_one("#prompt-text", TextArea)
        area.replace(chosen + " ", start, end)
        area.focus()
