"""the Settings screen -- FONT (desk only, writes Windows Terminal's `settings.json`
through `wt.py`) and PALETTE (preset + per-token overrides through `palette.py`).

Two ways in, same as `pantheon.tools.app`:
- pure functions (`snapshot`, `apply_font`, `apply_preset_action`, ...) that `bin/appearance_show`
  / `bin/appearance_apply` and the tests drive without a terminal;
- `AppearanceApp`, the standalone `pantheon --settings` window.

FONT applies on its own button. PALETTE applies live: every preset pick or override lands on disk and repaints
this window immediately, no separate step.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Static

from .. import config as config_mod, keys as keys_mod, orphan, theme as theme_mod
from ..widgets.footer import PhoneFooter, WindowBar, fit_footer, make_footer
from . import palette, wt

log = logging.getLogger("pantheon.appearance")

# The Windows Terminal profile this build guesses is "the Pantheon one": matched by NAME first, then by `commandline` containing this text -- `wt.py`
# falls back to `profiles.defaults` and every screen/report below says which it used, so an
# ambiguous or wrong guess is never silent.
DEFAULT_PROFILE_NAME = "Pantheon"

# Monospace faces offered on the Settings screen, best first; the first entry is the default.
FONT_CHOICES = (
    "JetBrains Mono",
    "Cascadia Code",
    "Fira Code",
    "IBM Plex Mono",
    "Geist Mono",
    "Red Hat Mono",
    "DM Mono",
    "Martian Mono",
    "Sometype Mono",
    "Azeret Mono",
    "Ubuntu Sans Mono",
    "Roboto Mono",
    "Cascadia Mono",
    "Consolas",
)

CELL_MIN, CELL_MAX = 0.8, 1.5   # font.cellHeight / font.cellWidth, per the spec's schema research
SIZE_MIN, SIZE_MAX = 6.0, 32.0  # a sane guard on font.size; WT itself imposes no fixed bound


def _toml_path(cfg) -> Path:
    """Where Settings writes -- the project's own `pantheon.toml`, same file `cfg` was loaded
    from in every real run (`config.load()` always reads `config.DEFAULT_TOML` unless a test
    points it elsewhere, which is why every function below takes `toml_path` explicitly rather
    than assuming this)."""
    return config_mod.DEFAULT_TOML


# ---- pure logic (testable without a terminal) --------------------------------------------------

def snapshot(cfg=None, toml_path: Optional[Path] = None, profile_name: str = DEFAULT_PROFILE_NAME) -> dict:
    """Everything the screen and `bin/appearance_show` need: the resolved font (read from
    Windows Terminal when found, else the last-applied values remembered in `pantheon.toml`) and
    the active palette. Never raises -- a missing WT file or a fresh `pantheon.toml` both come
    back as honest "not set" values, same discipline as `tools.app.build_snapshot`."""
    cfg = cfg or config_mod.load()
    toml_path = toml_path or _toml_path(cfg)
    appearance = cfg.appearance_settings()
    theme_cfg = cfg.theme_settings()

    wt_path = wt.find_settings()
    font_report: dict = {
        "source": "none",
        "message": wt.NOT_FOUND_MESSAGE,
        "face": appearance.font_face or None,
        "size": appearance.font_size or None,
        "cell_height": appearance.cell_height or None,
        "cell_width": appearance.cell_width or None,
        "wt_profile": None,
    }
    if wt_path is not None:
        try:
            live = wt.read_font(wt_path, profile_name)
            font_report.update(
                source="windows_terminal",
                message=None,
                face=live.face,
                size=live.size,
                cell_height=live.cell_height,
                cell_width=live.cell_width,
            )
            remembered = (appearance.font_face or None, appearance.font_size or None,
                          appearance.cell_height or None, appearance.cell_width or None)
            live_tuple = (live.face, live.size, live.cell_height, live.cell_width)
            if any(remembered) and remembered != live_tuple:
                font_report["message"] = (
                    "Windows Terminal's file has different values than the last apply from here "
                    "-- showing what the file actually has."
                )
        except Exception as exc:  # a corrupt/unreadable file must not blank the whole screen
            font_report["message"] = f"could not read Windows Terminal's settings.json: {exc}"

    resolved = palette.resolved_tokens(theme_cfg.preset, theme_cfg.overrides)
    return {
        "font": font_report,
        "palette": {
            "preset": theme_cfg.preset,
            "overrides": dict(theme_cfg.overrides),
            "resolved": resolved,
            "presets": palette.PRESET_NAMES,
            "tokens": palette.TOKEN_NAMES,
        },
    }


def apply_font(
    cfg=None,
    toml_path: Optional[Path] = None,
    profile_name: str = DEFAULT_PROFILE_NAME,
    face: Optional[str] = None,
    size: Optional[float] = None,
    cell_height: Optional[float] = None,
    cell_width: Optional[float] = None,
) -> dict:
    """The FONT section's Apply button / `bin/appearance_apply --font-*`. Writes Windows
    Terminal's file (backup + surgical patch + reparse-or-restore, `wt.write_font`), then
    remembers the values in `pantheon.toml [appearance]` so the screen shows them next time even
    before re-reading WT. `{"ok": False, "message": ...}` off-desk/off-Windows -- nothing is
    touched."""
    cfg = cfg or config_mod.load()
    toml_path = toml_path or _toml_path(cfg)
    wt_path = wt.find_settings()
    if wt_path is None:
        return {"ok": False, "message": wt.NOT_FOUND_MESSAGE}
    font = wt.Font(face=face, size=size, cell_height=cell_height, cell_width=cell_width)
    try:
        report = wt.write_font(wt_path, profile_name, font, state_dir=cfg.state_dir)
    except wt.WriteError as exc:
        return {"ok": False, "message": str(exc)}
    remembered = {}
    if face is not None:
        remembered["font_face"] = face
    if size is not None:
        remembered["font_size"] = float(size)
    if cell_height is not None:
        remembered["cell_height"] = float(cell_height)
    if cell_width is not None:
        remembered["cell_width"] = float(cell_width)
    if remembered:
        palette.write_appearance_font(toml_path, remembered)
    return {
        "ok": True,
        "profile": report["profile"],
        "backup": str(report["backup"]),
        "changes": report["changes"],
        "message": (
            f"written to Windows Terminal ({report['profile']} profile); it redraws on save -- "
            "look at the desk"
        ),
    }


def apply_preset_action(name: str, cfg=None, toml_path: Optional[Path] = None) -> dict:
    cfg = cfg or config_mod.load()
    toml_path = toml_path or _toml_path(cfg)
    resolved = palette.apply_preset(toml_path, name)
    return {"preset": name, "resolved": resolved}


def override_action(token: str, value: str, cfg=None, toml_path: Optional[Path] = None) -> dict:
    cfg = cfg or config_mod.load()
    toml_path = toml_path or _toml_path(cfg)
    resolved = palette.override(toml_path, token, value)
    return {"token": token, "value": value, "resolved": resolved}


def reset_token_action(token: str, cfg=None, toml_path: Optional[Path] = None) -> dict:
    cfg = cfg or config_mod.load()
    toml_path = toml_path or _toml_path(cfg)
    resolved = palette.reset_token(toml_path, token)
    return {"token": token, "resolved": resolved}


def reset_all_action(cfg=None, toml_path: Optional[Path] = None) -> dict:
    cfg = cfg or config_mod.load()
    toml_path = toml_path or _toml_path(cfg)
    resolved = palette.reset_all(toml_path)
    return {"resolved": resolved}


# ---- the screen ---------------------------------------------------------------------------------

HELP_TEXT = "Tab move    Enter/Space choose    Esc close advanced    ? this help    q close"


class AppearanceApp(App):
    """`pantheon --settings`: FONT (desk only) and PALETTE, ."""

    CSS = """
    Screen { background: $background; }
    #appearance-scroll { padding: 1 2; height: 1fr; }
    .section-title { text-style: bold; color: $primary; margin-top: 1; }
    .row { height: auto; margin-bottom: 1; }
    .stepper { width: auto; margin-right: 1; }
    #font-status, #palette-status { padding: 0 1; height: auto; color: $text-muted; }
    #appearance-help { padding: 0 1; height: 1; background: $panel; color: $text-muted; }
    .token-row { height: auto; }
    .token-name { width: 20; }
    .token-input { width: 14; }
    """
    BINDINGS = [
        ("question_mark", "help", "keys"),
        ("q", "quit", "quit"),
    ]

    def __init__(self, cfg=None, profile_name: str = DEFAULT_PROFILE_NAME) -> None:
        super().__init__()
        self.cfg = cfg or config_mod.load()
        self.profile_name = profile_name
        self._font_face = None
        self._font_size = None
        self._cell_height = None
        self._cell_width = None
        # What `redraw()` last told the preset `RadioSet` to show -- `on_radio_set_changed`
        # compares against this to tell a real click apart from Textual echoing the sync back
        # (see `redraw()`'s docstring comment for why a boolean "suspend" flag does not work).
        self._known_preset = None

    def compose(self) -> ComposeResult:
        yield WindowBar("appearance")
        with VerticalScroll(id="appearance-scroll"):
            yield Static("FONT (desk only -- writes Windows Terminal)", classes="section-title")
            yield Static("", id="font-body")
            with Horizontal(classes="row"):
                yield Label("face:")
                yield RadioSet(*[RadioButton(f) for f in FONT_CHOICES], id="font-face")
            with Horizontal(classes="row"):
                yield Label("size")
                yield Button("-", id="size-down", classes="stepper")
                yield Static("", id="size-value", classes="stepper")
                yield Button("+", id="size-up", classes="stepper")
                yield Label("line height")
                yield Button("-", id="cellh-down", classes="stepper")
                yield Static("", id="cellh-value", classes="stepper")
                yield Button("+", id="cellh-up", classes="stepper")
                yield Label("cell width")
                yield Button("-", id="cellw-down", classes="stepper")
                yield Static("", id="cellw-value", classes="stepper")
                yield Button("+", id="cellw-up", classes="stepper")
            yield Button("Apply font", id="font-apply", variant="primary")
            yield Static("", id="font-status")

            yield Static("PALETTE", classes="section-title")
            yield RadioSet(*[RadioButton(p) for p in palette.PRESET_NAMES], id="preset-picker")
            yield Button("Advanced: per-token overrides", id="advanced-toggle")
            with Vertical(id="advanced-body"):
                for token in palette.TOKEN_NAMES:
                    with Horizontal(classes="token-row"):
                        yield Label(token, classes="token-name")
                        yield Input(placeholder="#RRGGBB", id=f"tok-{token}", classes="token-input")
                        yield Button("reset", id=f"reset-{token}")
                yield Button("Reset all", id="reset-all", variant="error")
            yield Static("", id="palette-status")
        yield PhoneFooter("appearance")
        yield Static(HELP_TEXT, id="appearance-help")
        yield make_footer()

    def on_mount(self) -> None:
        theme_mod.apply(self)
        self.title = "PANTHEON - settings"
        self.query_one("#advanced-body", Vertical).display = False
        fit_footer(self, self.size.width)
        self.redraw()
        orphan.install(self)

    def on_resize(self, event) -> None:
        fit_footer(self, event.size.width)

    # ---- drawing -----------------------------------------------------------
    def redraw(self) -> None:
        try:
            snap = snapshot(self.cfg, profile_name=self.profile_name)
        except Exception as exc:
            self.show_font_status("Could not read appearance settings; details in the log file.")
            self._log_traceback(exc)
            return
        font = snap["font"]
        self._font_face = font["face"]
        self._font_size = font["size"] or 11.0
        self._cell_height = font["cell_height"] or 1.0
        self._cell_width = font["cell_width"] or 1.0
        body = self.query_one("#font-body", Static)
        if font["source"] == "none":
            body.update(font["message"])
        else:
            body.update(f"current: {font['face']}  size {font['size']}  "
                        f"line height {font['cell_height']}  cell width {font['cell_width']}")
            if font["message"]:
                self.show_font_status(font["message"])
        self._set_stepper_labels()

        pal = snap["palette"]
        # `self._known_preset` is how `on_radio_set_changed` tells a real click apart from
        # Textual echoing this sync back to us (or a `RadioSet` picking its own default the
        # moment it mounts) -- a `try/finally`-scoped "suspend the handler" flag does NOT work
        # here: `RadioButton.value = True` posts a message that Textual delivers on a LATER pump
        # cycle, after this synchronous method has already returned and any such flag has already
        # been reset (found while testing: the delayed delivery slipped past exactly that guard
        # and wrote `[theme] preset = "pantheon"` into the REAL project pantheon.toml on mount,
        # no click involved -- reverted before this build shipped). Comparing against "what did
        # we just tell the UI to show" is correct regardless of when the message actually arrives.
        self._known_preset = pal["preset"]
        try:
            face_set = self.query_one("#font-face", RadioSet)
            for button in face_set.children:
                if isinstance(button, RadioButton) and str(button.label) == self._font_face:
                    button.value = True
        except Exception:
            pass
        try:
            preset_set = self.query_one("#preset-picker", RadioSet)
            for button in preset_set.children:
                if isinstance(button, RadioButton) and str(button.label) == pal["preset"]:
                    button.value = True
        except Exception:
            pass

        for token in palette.TOKEN_NAMES:
            try:
                box = self.query_one(f"#tok-{token}", Input)
                box.value = pal["overrides"].get(token, "")
                box.placeholder = pal["resolved"][token]
            except Exception:
                pass
        self._apply_live_theme(pal["resolved"], pal["preset"])

    def _apply_live_theme(self, resolved: dict, preset_name: str) -> None:
        dark = palette.PRESET_DARK.get(preset_name, True)
        try:
            self.register_theme(palette.make_theme("pantheon-custom", resolved, dark=dark))
            self.theme = "pantheon-custom"
        except Exception as exc:  # a bad hex value must not crash the whole screen
            self.show_palette_status(f"could not preview this palette: {exc}")

    def _set_stepper_labels(self) -> None:
        self.query_one("#size-value", Static).update(f"{self._font_size:g}")
        self.query_one("#cellh-value", Static).update(f"{self._cell_height:.2f}")
        self.query_one("#cellw-value", Static).update(f"{self._cell_width:.2f}")

    def show_font_status(self, text: str) -> None:
        try:
            self.query_one("#font-status", Static).update(text)
        except Exception:
            pass

    def show_palette_status(self, text: str) -> None:
        try:
            self.query_one("#palette-status", Static).update(text)
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

    # ---- FONT keys -----------------------------------------------------------
    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "font-face" and event.pressed is not None:
            self._font_face = str(event.pressed.label)
        elif event.radio_set.id == "preset-picker" and event.pressed is not None:
            name = str(event.pressed.label)
            if name == self._known_preset:
                return  # redraw()'s own sync echoing back, or the RadioSet's initial default -- not a click
            try:
                result = apply_preset_action(name, self.cfg)
            except palette.UnknownPresetError as exc:
                self.show_palette_status(str(exc))
                return
            self._known_preset = name
            self.show_palette_status(f"palette: {name}")
            self._apply_live_theme(result["resolved"], name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "size-up":
            self._font_size = min(SIZE_MAX, (self._font_size or 11.0) + 1)
            self._set_stepper_labels()
        elif bid == "size-down":
            self._font_size = max(SIZE_MIN, (self._font_size or 11.0) - 1)
            self._set_stepper_labels()
        elif bid == "cellh-up":
            self._cell_height = round(min(CELL_MAX, (self._cell_height or 1.0) + 0.05), 2)
            self._set_stepper_labels()
        elif bid == "cellh-down":
            self._cell_height = round(max(CELL_MIN, (self._cell_height or 1.0) - 0.05), 2)
            self._set_stepper_labels()
        elif bid == "cellw-up":
            self._cell_width = round(min(CELL_MAX, (self._cell_width or 1.0) + 0.05), 2)
            self._set_stepper_labels()
        elif bid == "cellw-down":
            self._cell_width = round(max(CELL_MIN, (self._cell_width or 1.0) - 0.05), 2)
            self._set_stepper_labels()
        elif bid == "font-apply":
            self._do_apply_font()
        elif bid == "advanced-toggle":
            body = self.query_one("#advanced-body", Vertical)
            body.display = not body.display
        elif bid == "reset-all":
            result = reset_all_action(self.cfg)
            self.show_palette_status("every token override cleared")
            preset = self.query_one("#preset-picker", RadioSet)
            active = next((str(b.label) for b in preset.children if isinstance(b, RadioButton) and b.value), "pantheon")
            self._apply_live_theme(result["resolved"], active)
            self.redraw()
        elif bid.startswith("reset-"):
            token = bid[len("reset-"):]
            result = reset_token_action(token, self.cfg)
            self.show_palette_status(f"{token}: back to the preset value")
            preset = self.query_one("#preset-picker", RadioSet)
            active = next((str(b.label) for b in preset.children if isinstance(b, RadioButton) and b.value), "pantheon")
            self._apply_live_theme(result["resolved"], active)
            self.redraw()

    def _do_apply_font(self) -> None:
        result = apply_font(
            self.cfg,
            profile_name=self.profile_name,
            face=self._font_face,
            size=self._font_size,
            cell_height=self._cell_height,
            cell_width=self._cell_width,
        )
        self.show_font_status(result["message"])
        if result["ok"]:
            self.redraw()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id and event.input.id.startswith("tok-"):
            token = event.input.id[len("tok-"):]
            value = event.value.strip()
            if not value:
                return
            try:
                result = override_action(token, value, self.cfg)
            except palette.UnknownTokenError as exc:
                self.show_palette_status(str(exc))
                return
            self.show_palette_status(f"{token} -> {value}")
            preset = self.query_one("#preset-picker", RadioSet)
            active = next((str(b.label) for b in preset.children if isinstance(b, RadioButton) and b.value), "pantheon")
            self._apply_live_theme(result["resolved"], active)

    def action_help(self) -> None:
        self.show_palette_status(keys_mod.section("appearance"))


def main() -> int:
    cfg = config_mod.load()
    AppearanceApp(cfg).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
