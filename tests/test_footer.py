"""The phone footer: under 80 columns every window shows a
hand-written four-key line instead of Textual's footer, which at 65 columns squeezed the keys
until `open`, `refresh`, `Esc`, `?` and `q` fell off the edge; at 80 and wider Textual's footer is back, compact and without the palette entry."""
from __future__ import annotations

import asyncio
import functools
import shutil
from pathlib import Path

from textual.widgets import Footer

from pantheon import config as config_mod
from pantheon.deck.app import DeckApp
from pantheon.hud.app import HudApp
from pantheon.queue.app import QueueApp
from pantheon.supervisor.app import SupervisorApp
from pantheon.tasks import obsidian_base as ob
from pantheon.widgets.footer import PHONE_KEYS, PhoneFooter

FIXTURES = Path(__file__).parent / "fixtures"


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


def _cfg(tmp_path: Path) -> config_mod.Config:
    projects = tmp_path / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    cfg = config_mod.Config(vault=str(tmp_path), state_dir=str(tmp_path / "state"),
                            projects_root="C:/Home/x/Documents/Projects")
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    return cfg


def _apps(cfg):
    return {
        "deck": DeckApp(cfg, window_source=lambda: [], source=ob.make(cfg)),
        "queue": QueueApp(cfg, source=ob.make(cfg), window_source=lambda: []),
        "supervisor": SupervisorApp(cfg, window_source=lambda: []),
        "hud": HudApp(cfg, start_refresher=False),
    }


def _shown(app) -> tuple[bool, bool]:
    phone = app.query_one(PhoneFooter).display
    textual = any(f.display for f in app.query(Footer))
    return phone, textual


@drives_the_screen
async def test_phone_width_shows_the_four_key_line_in_every_window(tmp_path):
    cfg = _cfg(tmp_path)
    for name, app in _apps(cfg).items():
        async with app.run_test(size=(65, 26)) as pilot:
            await pilot.pause()
            phone, textual = _shown(app)
            assert phone and not textual, f"{name}: phone line expected at 65 columns"
            assert str(app.query_one(PhoneFooter).content) == PHONE_KEYS[name]


@drives_the_screen
async def test_desk_width_uses_textuals_footer_without_the_palette_entry(tmp_path):
    cfg = _cfg(tmp_path)
    for name, app in _apps(cfg).items():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            phone, textual = _shown(app)
            assert textual and not phone, f"{name}: Textual's footer expected at 120 columns"
            footer = app.query_one(Footer)
            assert footer.show_command_palette is False and footer.compact is True


@drives_the_screen
async def test_phone_header_lists_every_tab_with_its_number_key(tmp_path):
    """ -- the phone header showed only the current tab. Now it lists all of them with the
    number key that jumps to each, the current one in brackets."""
    from textual.widgets import Static

    cfg = _cfg(tmp_path)
    app = _apps(cfg)["queue"]
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.pause()
        header = str(app.pane.query_one("#header", Static).content)
        assert "[1 Now " in header
        # Every view of the fixture Base is on the strip, each behind its number key (the order
        # is the Base's own, so only the names are asserted here).
        for name in ("Quick", "Decide", "Plate", "Someday", "Notes", "Project", "Mine"):
            assert f" {name} " in header, header
        # The strip breaks only between tabs and fits the header's 63 content columns: left to
        # the widget, it split `6 Notes 52` across two lines.
        import re
        for strip_line in header.split("\n")[1:]:
            assert len(strip_line) <= 63, strip_line
            for cell in strip_line.split("  "):
                assert re.match(r"^\[?\d \S+ \d+\]?$", cell), (cell, strip_line)
        await pilot.press("3")
        await pilot.pause()
        header = str(app.pane.query_one("#header", Static).content)
        assert "[3 " in header and "[1 Now " not in header


@drives_the_screen
async def test_footer_follows_a_resize(tmp_path):
    cfg = _cfg(tmp_path)
    app = _apps(cfg)["queue"]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert _shown(app) == (False, True)
        await pilot.resize_terminal(65, 26)
        await pilot.pause()
        assert _shown(app) == (True, False)
