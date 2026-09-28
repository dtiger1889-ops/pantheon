"""`python -m pantheon.session_view.demo` -- one expanded `SessionTile` full of hand-made items,
so the tile can be looked at by a person instead of only asserted about.

Nothing here reads a real transcript or touches tmux: the conversation is built out of
`models.py` by hand, exactly the way `tests/test_session_tile.py` does. Typing in the message
box will try a real `tmux send-keys` against `pantheon:3`, which simply fails when that window
does not exist.

    python -m pantheon.session_view.demo              # interactive, in this terminal
    python -m pantheon.session_view.demo --render     # write docs/renders/s26-tile-demo.txt
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

from rich.console import Console

from textual.app import App, ComposeResult

from .. import theme as theme_mod
from . import models as m
from .tile import SessionTile

RENDER_PATH = Path(__file__).resolve().parents[2] / "docs" / "renders" / "s26-tile-demo.txt"
RENDER_SIZE = (200, 50)


def demo_conversation() -> m.Conversation:
    """A believable slice of a real session: a prompt, a thought, three tool calls (one still
    running, one failed), prose, a hook receipt and a subagent."""
    items = (
        m.Item(kind=m.USER, uuid="u1",
               text="the launcher dies when Termius opens a shell -- find out why"),
        m.Item(kind=m.THINKING, uuid="k1",
               text="The tmux server would have to live in Windows session 0 for sshd's shells "
                    "to reach it. If the deck started it from the Desktop app that is session 1, "
                    "and the socket is invisible."),
        m.Item(kind=m.TOOL, uuid="t1", tool=m.ToolCall(
            id="toolu_1", name="Read", summary="Read CHECKPOINT.md", file="CHECKPOINT.md",
            input_text="{'file_path': 'C:/Home/x/Documents/Projects/project_lanterns/CHECKPOINT.md'}",
            result_text="Last updated: 2026-09-06\nOpen threads: launcher gate, S25 codex runner…")),
        m.Item(kind=m.TOOL, uuid="t2", tool=m.ToolCall(
            id="toolu_2", name="Bash", summary="Bash tmux list-sessions",
            input_text="tmux list-sessions", result_text="no server running on /tmp/tmux-1000/default",
            is_error=True)),
        m.Item(kind=m.ASSISTANT, uuid="a1", text=(
            "The socket is the whole story. `sshd` spawns your phone shells in **Windows session "
            "0**; a tmux server started from the Desktop app lives in session 1, and two MSYS2 "
            "processes in different Windows sessions cannot share a socket. So `tmux new-session "
            "-A` finds nothing to attach to and the login shell exits — which is exactly the "
            "\"Termius closes immediately\" you saw.\n\n"
            "Start `pantheon` from Termius or the desktop icon and the deck comes back.")),
        m.Item(kind=m.SYSTEM, uuid="s1", text="hook: pantheon_event",
               detail="PreToolUse pantheon_event.ps1 -> wrote 1 event to state/events.jsonl"),
        m.Item(kind=m.SUBAGENT, uuid="g1", text="check the sshd service account",
               detail="Sonnet, 9 turns: sshd runs as LocalSystem in session 0; confirmed with "
                      "`sc qc sshd` and `tasklist /svc`.", sidechain=True),
        m.Item(kind=m.TOOL, uuid="t3", tool=m.ToolCall(
            id="toolu_3", name="Bash", summary="Bash python -m pytest -q tests/test_launcher_tmux.py",
            input_text="python -m pytest -q tests/test_launcher_tmux.py", result_text=None)),
    )
    return m.Conversation(
        session_id="aaa11111-2222-3333-4444-555555555555",
        path="~/.claude/projects/project_lanterns/aaa11111.jsonl",
        title="Why the launcher dies under Termius",
        model="claude-opus-5[1m]",
        effort="high",
        context_tokens=372_000,
        cwd="C:/Home/x/Documents/Projects/project_lanterns",
        git_branch="build/s26-tiles",
        items=items,
    )


def demo_entry() -> m.SessionEntry:
    return m.SessionEntry(
        session_id="aaa11111-2222-3333-4444-555555555555",
        group=m.LIVE,
        project="project_lanterns",
        cwd="C:/Home/x/Documents/Projects/project_lanterns",
        title="Why the launcher dies under Termius",
        status_text="needs you",
        style="warning",
        needs_human=True,
        window_index=3,
        tmux_session="pantheon",
        transcript_path="~/.claude/projects/project_lanterns/aaa11111.jsonl",
    )


class TileDemo(App):
    """One expanded tile, nothing else -- so what you see is the widget, not the deck."""

    CSS = "Screen { background: #05080B; }"
    BINDINGS = [("q", "quit", "quit"), ("escape", "quit", "quit")]

    def compose(self) -> ComposeResult:
        yield SessionTile(demo_entry(), demo_conversation(), expanded=True, id="demo-tile")

    def on_mount(self) -> None:
        theme_mod.apply(self)
        self.query_one("#demo-tile", SessionTile).focus()


def _text_dump(app, width: int, height: int) -> str:
    """The screen as plain characters, so column alignment and empty space stay visible -- the
    same technique `docs/renders/render_desk.py` uses for the deck renders."""
    console = Console(record=True, width=width, height=height, file=io.StringIO(),
                      legacy_windows=False, color_system="truecolor")
    console.print(app.screen._compositor)
    return console.export_text(styles=False)


def render(path: Path = RENDER_PATH, size=RENDER_SIZE) -> str:
    """Draw the demo headlessly and write the text (and an SVG beside it) to `docs/renders/`."""
    width, height = size
    app = TileDemo()
    out = {}

    async def go():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.pause()
            app.save_screenshot(path.with_suffix(".svg").name, path=str(path.parent))
            out["text"] = _text_dump(app, width, height)

    path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(go())
    path.write_text(out["text"], encoding="utf-8")
    return out["text"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pantheon.session_view.demo", description=__doc__)
    ap.add_argument("--render", action="store_true",
                    help="draw it headlessly and write docs/renders/s26-tile-demo.txt")
    args = ap.parse_args(argv)
    if args.render:
        render()
        print(f"wrote {RENDER_PATH}")
        return 0
    TileDemo().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
