"""Starting an agent from a queue row: the `w` and `x` keys, the guardrails, and the
`dispatched to ...` line that appears under a row once an agent is on it.

No real agent is ever started here: the provider is a stand-in that records what it was asked to
do, and the tmux window list is always empty, so nothing outside `tmp_path` is touched.
"""
from __future__ import annotations

import asyncio
import functools
import json
import shutil
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from pantheon import config as config_mod
from pantheon.models import LaunchResult, utcnow_iso
from pantheon.queue import pane as pane_mod
from pantheon.queue.app import QueueApp

FIXTURES = Path(__file__).parent / "fixtures"

# Folders that exist under the test projects root. `garden` is deliberately missing, so a garden
# row is the one that gets refused for having nowhere to work.
REAL_PROJECTS = ("workshop", "archive", "household")


def drives_the_screen(test):
    """Runs an async test without needing an extra pytest plugin installed."""
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


class StandIn:
    """A provider that answers like the real adapters but starts nothing."""

    def __init__(self, name: str = "claude", ok: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.calls: list[tuple[str, str, bool]] = []

    def capabilities(self) -> set[str]:
        return {"interactive_tmux"} if self.name == "claude" else {"headless"}

    def launch(self, project_dir: str, briefing_path: str, interactive: bool) -> LaunchResult:
        self.calls.append((project_dir, briefing_path, interactive))
        return LaunchResult(ok=self.ok, where="tmux", window_index=3, tmux_pane="%9",
                            message=f"{self.name} is working in window 3")


def _cfg(tmp_path: Path) -> config_mod.Config:
    vault = tmp_path / "vault"
    projects_in_vault = vault / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects_in_vault / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects_in_vault / "Sprints.base")
    root = tmp_path / "code"
    for name in REAL_PROJECTS:
        (root / name).mkdir(parents=True)
    return config_mod.Config(vault=str(vault), state_dir=str(tmp_path / "state"),
                             projects_root=str(root))


def _busy_agents(cfg: config_mod.Config, how_many: int) -> None:
    """Write an event log that makes `how_many` agents look like they are working right now."""
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.events_file, "w", encoding="utf-8") as fh:
        for i in range(how_many):
            fh.write(json.dumps({
                "ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
                "session_id": f"session{i}0000000", "cwd": f"C:/Home/x/Documents/Projects/p{i}",
            }) + "\n")


def _app(tmp_path: Path, monkeypatch, cfg=None, providers=None) -> QueueApp:
    from pantheon.tasks import obsidian_base as ob

    cfg = cfg or _cfg(tmp_path)
    monkeypatch.setattr(pane_mod, "get_providers", lambda c: providers or {})
    return QueueApp(cfg, source=ob.make(cfg), window_source=lambda: [])


async def _pick(pilot, needle: str) -> None:
    """Land the highlight on the one open row whose text holds `needle`."""
    await pilot.press("7")            # "By project": every open row
    await pilot.press("slash")
    await pilot.pause()
    await pilot.press(*needle)
    await pilot.pause()
    await pilot.press("enter")        # hands the keyboard back from the search box to the list
    await pilot.pause()


def _body(app: QueueApp) -> str:
    from textual.widgets import Static
    return str(app.query_one("#list", Static).content)


def _dispatch_events(cfg: config_mod.Config) -> list[dict]:
    if not cfg.events_file.exists():
        return []
    lines = cfg.events_file.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip() and '"dispatch"' in line]


# ---------------------------------------------------------------- w: work this task


@drives_the_screen
async def test_w_writes_a_briefing_launches_and_records_one_dispatch(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    claude = StandIn("claude")
    app = _app(tmp_path, monkeypatch, cfg=cfg, providers={"claude": claude})
    async with app.run_test(size=(120, 40)) as pilot:
        await _pick(pilot, "newcomer")          # Summarise the workshop notes (workshop, Claude's)
        assert app.pane.selected_row().id == "Summarise the workshop notes"
        await pilot.press("w")
        await app.workers.wait_for_complete()
        await pilot.pause()

        briefings = sorted(cfg.dispatch_dir.glob("*.md"))
        assert len(briefings) == 1
        assert "Summarise the workshop notes" in briefings[0].read_text(encoding="utf-8")
        assert len(claude.calls) == 1
        project_dir, briefing_path, interactive = claude.calls[0]
        assert project_dir.endswith("workshop") and briefing_path == str(briefings[0])
        assert interactive is True                      # the claude adapter runs in tmux
        assert len(_dispatch_events(cfg)) == 1
        assert app.pane.message == "claude is working in window 3"


@drives_the_screen
async def test_w_on_a_row_with_no_project_folder_says_so_and_starts_nothing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    claude = StandIn("claude")
    app = _app(tmp_path, monkeypatch, cfg=cfg, providers={"claude": claude})
    async with app.run_test(size=(120, 40)) as pilot:
        await _pick(pilot, "listing")           # Trial a seed-swap listing site (garden: no folder)
        assert app.pane.selected_row().project == "garden"
        await pilot.press("w")
        await pilot.pause()
        assert app.pane.message == "no project folder for 'garden'; open it by hand"
        assert claude.calls == []
        assert list(cfg.dispatch_dir.glob("*.md")) == []
        assert _dispatch_events(cfg) == []


@drives_the_screen
async def test_a_row_on_the_users_queue_asks_first_and_n_leaves_it_alone(tmp_path, monkeypatch):
    """the old inline `y = yes, n = no` footer question is now a `Confirm` modal --
    `y`/`n` still answer it, exactly as the user already knew."""
    from pantheon.widgets.modal import Confirm
    from textual.widgets import Button

    cfg = _cfg(tmp_path)
    claude = StandIn("claude")
    app = _app(tmp_path, monkeypatch, cfg=cfg, providers={"claude": claude})
    async with app.run_test(size=(120, 40)) as pilot:
        await _pick(pilot, "enclosures")        # Relabel the spare backup drive (agent: "false")
        assert app.pane.selected_row().agent is not True
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        question = str(app.screen.query_one("#question", Static).content)
        assert "not the Agent's plate" in question
        # Not destructive (copy review "interaction issue 1"): Yes stays primary, not red.
        assert app.screen.query_one("#yes", Button).variant == "primary"
        assert claude.calls == []

        await pilot.press("n")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.pane.message == "left that task alone"
        assert claude.calls == []

        await pilot.press("w")
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(claude.calls) == 1
        assert app.pane.message == "claude is working in window 3"


@drives_the_screen
async def test_a_dispatched_row_says_so_under_it(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    app = _app(tmp_path, monkeypatch, cfg=cfg, providers={"claude": StandIn("claude")})
    async with app.run_test(size=(120, 40)) as pilot:
        await _pick(pilot, "newcomer")
        await pilot.press("w")
        await app.workers.wait_for_complete()
        await pilot.pause()
        body = _body(app)
        assert "dispatched to claude, window 3," in body
        assert "ago" in body


# ---------------------------------------------------------------- x: choose who works it


@drives_the_screen
async def test_x_then_i_starts_codex_in_a_pc_window(tmp_path, monkeypatch):
    """the old inline `c`/`x`/`i`/`l` prompt is now a `Pick` modal; the same letters
    still choose the same thing."""
    from pantheon.widgets.modal import Pick

    cfg = _cfg(tmp_path)
    codex = StandIn("codex")
    app = _app(tmp_path, monkeypatch, cfg=cfg,
               providers={"claude": StandIn("claude"), "codex": codex})
    async with app.run_test(size=(120, 40)) as pilot:
        await _pick(pilot, "newcomer")
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, Pick)
        await pilot.press("i")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(codex.calls) == 1
        assert codex.calls[0][2] is True                 # asked for a screen, not a headless job
        assert app.pane.message == "codex is working in window 3"


# ---------------------------------------------------------------- the guardrails


@drives_the_screen
async def test_the_fifth_agent_is_refused_with_the_count(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _busy_agents(cfg, 4)
    claude = StandIn("claude")
    app = _app(tmp_path, monkeypatch, cfg=cfg, providers={"claude": claude})
    async with app.run_test(size=(120, 40)) as pilot:
        await _pick(pilot, "newcomer")
        await pilot.press("w")
        await pilot.pause()
        assert "4 agents are already working" in app.pane.message
        assert "limit 4" in app.pane.message
        assert claude.calls == []


# ---------------------------------------------------------------- the status message wraps


@drives_the_screen
async def test_a_long_status_message_wraps_instead_of_clipping(tmp_path, monkeypatch):
    """The footer sentence (`#message`, `QueuePane._say`) must never lose words off the edge of
    a 65-column phone screen. `height: auto` is already on `#message`
    in the CSS; this proves it actually wraps rather than clipping, by re-joining the wrapped
    lines and checking they still spell out the whole sentence, word for word."""
    cfg = _cfg(tmp_path)
    app = _app(tmp_path, monkeypatch, cfg=cfg, providers={})
    long_message = (
        "codex job in window 4 is still catching up on a very long project name "
        "that will not fit on one line of a 65-column phone screen"
    )
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.pause()
        app.pane._say(long_message)
        await pilot.pause()
        widget = app.query_one("#message", Static)
        assert widget.size.height > 1              # wrapped onto more than one line, not clipped
        lines = [widget.render_line(i).text for i in range(widget.size.height)]
        assert " ".join(line.strip() for line in lines) == long_message


def test_the_dispatched_line_uses_a_plain_arrow_when_the_config_asks_for_it(tmp_path):
    """Design rule R6: a terminal that cannot draw the elbow still gets a readable line."""
    plain = config_mod.Config(state_dir=str(tmp_path / "state"), appearance={"glyphs": "ascii"})
    fancy = config_mod.Config(state_dir=str(tmp_path / "state"), appearance={"glyphs": "unicode"})
    assert pane_mod.QueuePane(plain, source=None, window_source=lambda: [])._branch() == "->"
    assert pane_mod.QueuePane(fancy, source=None, window_source=lambda: [])._branch() == "↳"


# ---------------------------------------------------------------- the tab keys


class Host(App):
    """A stand-in for the combined desk view: the queue is one panel, not the whole screen."""

    def __init__(self, pane) -> None:
        super().__init__()
        self.pane = pane

    def compose(self) -> ComposeResult:
        yield self.pane


@drives_the_screen
async def test_the_bracket_keys_always_cycle_the_tabs(tmp_path, monkeypatch):
    from pantheon.tasks import obsidian_base as ob

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(pane_mod, "get_providers", lambda c: {})
    app = QueueApp(cfg, source=ob.make(cfg), window_source=lambda: [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.pane.tab.name == "Now"
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert app.pane.tab.name == "Decide"
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert app.pane.tab.name == "Now"


@drives_the_screen
async def test_tab_walks_the_tabs_only_when_the_queue_is_the_whole_screen(tmp_path, monkeypatch):
    """Beside another panel, Tab belongs to the screen so it can move between panels; the queue's
    own tab keys are then `]` and `[`, which never clash with anything."""
    from pantheon.tasks import obsidian_base as ob

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(pane_mod, "get_providers", lambda c: {})

    on_its_own = pane_mod.QueuePane(cfg, source=ob.make(cfg), standalone=True,
                                    window_source=lambda: [])
    async with Host(on_its_own).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert on_its_own.tab.name == "Now"
        await pilot.press("tab")
        await pilot.pause()
        assert on_its_own.tab.name == "Decide"

    beside_another_pane = pane_mod.QueuePane(cfg, source=ob.make(cfg), standalone=False,
                                             window_source=lambda: [])
    async with Host(beside_another_pane).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert beside_another_pane.tab.name == "Now"
        await pilot.press("tab")
        await pilot.pause()
        assert beside_another_pane.tab.name == "Now"
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert beside_another_pane.tab.name == "Decide"
