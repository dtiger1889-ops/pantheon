"""tools page: a read-only inventory of slash skills, guards and
hooks per model, with a Claude/Codex sync mark. Two ways in:

- `render_report` -- the plain-text rendering both this window and the deck's own `?` overlay
  (its third page, `pantheon/deck/app.py`) show, so the two never drift apart.
- `ToolsApp` -- the standalone `pantheon --tools` window (its own tmux window, opened on demand).

Read-only end to end: nothing here writes to `~/.claude`, `~/.codex`, or
`~/.agents/skills`, and no key does anything but re-read, sort, or filter what is already on
screen. Fixing an out-of-sync skill is `skill-sync`'s job, not this page's.
"""
from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from .. import config as config_mod, glyphs, orphan, theme
from ..widgets.footer import PhoneFooter, WindowBar, fit_footer, make_footer
from . import inventory

log = logging.getLogger("pantheon.tools")

# A hook `command` is a full `powershell.exe -File "C:/Users/.../hooks/guard.ps1"` line; the page
# names only the script, never the box's own path layout.
_CMD_FILENAME = re.compile(r"([^\\/\"]+\.(?:ps1|py|sh))\"?\s*$")

# `sync_state` values -> the glyph key in `glyphs.table` and the word printed beside it
#.
_SYNC_GLYPH = {
    "in sync": "ok",
    "claude only": "working",
    "codex only": "idle",
    "link broken": "fail",
}
SORTS = ("name", "sync")


def hook_filename(command: str) -> str:
    """The script filename out of a hook's command line, e.g. `guard.ps1` -- never the whole
    path, which is this box's own layout and not something the user needs repeated back to him."""
    m = _CMD_FILENAME.search(command or "")
    return m.group(1) if m else (command or "?").strip() or "?"


def sorted_skill_names(sync: dict, sort: str = "name") -> list[str]:
    names = list(sync.keys())
    if sort == "sync":
        return sorted(names, key=lambda n: (sync[n], n.lower()))
    return sorted(names, key=str.lower)


def filter_names(names: list[str], query: str) -> list[str]:
    q = (query or "").strip().lower()
    if not q:
        return list(names)
    return [n for n in names if q in n.lower()]


def build_snapshot(cfg) -> dict:
    """Everything the page and `bin/tools_show` need, read fresh every call -- these files
    change rarely, so there is no watcher and no cache. Never raises: a
    missing source comes back as an empty list/dict, reported as text by the callers below
, not as a traceback on the user's screen."""
    page = cfg.tools_page_settings()
    claude_skills = inventory.claude_skills(page.claude_skills_dir)
    codex_skills = inventory.codex_skills(page.agents_skills_dir)
    sync = inventory.sync_state(claude_skills, codex_skills)
    return {
        "skills": {
            "claude": {s.name: {"description": s.description, "path": s.path} for s in claude_skills},
            "codex": {s.name: {"description": s.description, "path": s.path, "target": s.target}
                      for s in codex_skills},
            "sync": sync,
        },
        "hooks": {
            "claude": [h.__dict__ for h in inventory.claude_hooks(page.claude_settings)],
            "codex": [h.__dict__ for h in inventory.codex_hooks(page.codex_hooks)],
        },
        "guards": {
            "claude": inventory.claude_guards(page.claude_settings).__dict__,
            "codex": inventory.codex_config(page.codex_config),
        },
    }


def _skills_lines(snap: dict, sort: str, query: str, glyph_mode: str) -> list[str]:
    g = glyphs.table(glyph_mode)
    sync = snap["skills"]["sync"]
    claude_by_name = snap["skills"]["claude"]
    names = filter_names(sorted_skill_names(sync, sort), query)
    lines = ["SKILLS", ""]
    if not sync:
        lines.append("no skills found")
        return lines
    if not names:
        lines.append(f"no skills matching '{query}'")
        return lines
    for name in names:
        state = sync.get(name, "claude only")
        mark = g.get(_SYNC_GLYPH.get(state, "idle"), "?")
        desc = (claude_by_name.get(name) or {}).get("description")
        if desc is None:
            desc = (snap["skills"]["codex"].get(name) or {}).get("description") or ""
        lines.append(f"{mark} {state:<12} {name} -- {desc}")
    return lines


def _hooks_lines(snap: dict) -> list[str]:
    lines = ["HOOKS", ""]
    for model, key in (("Claude", "claude"), ("Codex", "codex")):
        hooks = snap["hooks"][key]
        if not hooks:
            lines.append(f"{model} hooks: none found")
            continue
        for h in sorted(hooks, key=lambda h: (h["event"], h["matcher"])):
            matcher = h["matcher"] or "*"
            lines.append(f"{model} · {h['event']} · {hook_filename(h['command'])} (matcher: {matcher})")
    return lines


def _guards_lines(snap: dict) -> list[str]:
    lines = ["GUARDS", ""]
    deny = snap["guards"]["claude"].get("deny") or []
    lines.append("Claude permissions.deny:")
    if deny:
        lines.extend(f"  {d}" for d in deny)
    else:
        lines.append("  none found")
    codex_cfg = snap["guards"]["codex"]
    lines.append("")
    lines.append(f"Codex sandbox_mode: {codex_cfg.get('sandbox_mode') or 'none found'}")
    return lines


def render_report(cfg, sort: str = "name", query: str = "") -> str:
    """The whole page as plain text: SKILLS, then HOOKS, then GUARDS -- the exact three
    sections  names, in that order. Used both by `ToolsApp` and
    by the deck's `?` overlay third page."""
    snap = build_snapshot(cfg)
    glyph_mode = cfg.appearance_settings().glyphs
    lines = _skills_lines(snap, sort, query, glyph_mode)
    lines.append("")
    lines.extend(_hooks_lines(snap))
    lines.append("")
    lines.extend(_guards_lines(snap))
    return "\n".join(lines)


HELP_TEXT = (
    "r refresh    n sort by name    y sort by sync    / filter skills    "
    "Esc clear filter    ? this help    q close"
)


class ToolsApp(App):
    """`pantheon --tools`: one scrollable read-only window over `render_report()`."""

    CSS = """
    Screen { background: $background; }
    #tools-scroll { padding: 1 1; height: 1fr; }
    #tools-filter { display: none; margin: 0 1; }
    #tools-message { padding: 0 1; height: 1; color: #888888; }
    #tools-help { padding: 0 1; height: 1; background: $panel; color: $text-muted; }
    """
    BINDINGS = [
        ("r", "refresh", "refresh"),
        ("n", "sort_name", "sort name"),
        ("y", "sort_sync", "sort sync"),
        ("slash", "filter", "filter"),
        ("escape", "clear_filter", "clear filter"),
        ("question_mark", "help", "keys"),
        ("q", "quit", "quit"),
    ]

    def __init__(self, cfg=None) -> None:
        super().__init__()
        self.cfg = cfg or config_mod.load()
        self._sort = "name"
        self._query = ""

    def compose(self) -> ComposeResult:
        yield WindowBar("tools")
        with VerticalScroll(id="tools-scroll"):
            yield Static("", id="tools-body", markup=False)
        yield Input(placeholder="filter skills by name, Esc to clear", id="tools-filter")
        yield Static(HELP_TEXT, id="tools-help")
        yield Static("", id="tools-message")
        yield PhoneFooter("tools")
        yield make_footer()

    def on_mount(self) -> None:
        theme.apply(self)
        self.title = "PANTHEON - tools"
        fit_footer(self, self.size.width)
        self.redraw()
        orphan.install(self)  # leave when the tmux server is gone (pantheon/orphan.py)

    def on_resize(self, event) -> None:
        fit_footer(self, event.size.width)

    def redraw(self) -> None:
        try:
            text = render_report(self.cfg, sort=self._sort, query=self._query)
            self.query_one("#tools-body", Static).update(text)
        except Exception as exc:  # a bad source file must not blank the whole window
            self.show_message("Could not read the tool inventory; details in the log file.")
            self._log_traceback(exc)

    def show_message(self, text: str) -> None:
        try:
            self.query_one("#tools-message", Static).update(text)
        except Exception:
            pass

    def _log_traceback(self, exc: BaseException) -> None:
        try:
            path = Path(self.cfg.log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        except OSError:
            pass

    # ---- keys ------------------------------------------------------------
    def action_refresh(self) -> None:
        self.show_message("")
        self.redraw()

    def action_sort_name(self) -> None:
        self._sort = "name"
        self.redraw()

    def action_sort_sync(self) -> None:
        self._sort = "sync"
        self.redraw()

    def action_filter(self) -> None:
        box = self.query_one("#tools-filter", Input)
        box.display = True
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#tools-filter", Input)
        box.display = False
        box.value = ""
        self._query = ""
        self.redraw()
        self.query_one("#tools-scroll", VerticalScroll).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tools-filter":
            self._query = event.value
            self.redraw()

    def action_help(self) -> None:
        help_widget = self.query_one("#tools-help", Static)
        help_widget.display = not help_widget.display


def main() -> int:
    cfg = config_mod.load()
    ToolsApp(cfg).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
