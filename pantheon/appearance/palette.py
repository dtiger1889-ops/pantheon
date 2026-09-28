"""palette presets and per-token overrides.

Reuses the tokens `pantheon/theme.py` already defines -- this module never renames or removes a token, it only resolves
`(preset, overrides)` into a concrete colour for each one and persists the choice into
`pantheon.toml`'s `[theme]` table:

    [theme]
    preset = "high-contrast"

    [theme.overrides]
    chrome = "#33E0FF"

Palette stays Pantheon-internal: Windows Terminal's own
`colorScheme` is a separate thing the user can leave alone -- this never touches WT's file (that's
`wt.py`'s job, and only for the four font keys).

Palette applies live: `apply_preset`/`override`/`reset_token`/
`reset_all` write straight to disk and return the resolved tokens so a caller (the Settings
screen) can repaint immediately with `self.app.register_theme(make_theme(...))` +
`self.app.theme = ...` -- no separate "Apply" step, unlike the font side.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .. import theme as theme_mod

TOKEN_NAMES: tuple[str, ...] = tuple(theme_mod.TOKENS.keys())


class UnknownTokenError(ValueError):
    def __init__(self, token: str) -> None:
        super().__init__(
            f"unknown theme token {token!r} -- known tokens: {', '.join(TOKEN_NAMES)}"
        )
        self.token = token


class UnknownPresetError(ValueError):
    def __init__(self, name: str, names: tuple[str, ...]) -> None:
        super().__init__(f"unknown palette preset {name!r} -- known presets: {', '.join(names)}")
        self.name = name


def _with_overrides(overrides: dict) -> dict:
    base = dict(theme_mod.TOKENS)
    base.update(overrides)
    return base


# Every preset covers every token `theme.py` defines -- a preset
# that only changes a few tokens starts from a full copy of `theme.TOKENS` and layers its deltas
# on top, so nothing is ever missing.
PRESETS: dict[str, dict] = {
    "pantheon": dict(theme_mod.TOKENS),
    # Deltas match `theme.make_high_contrast_theme`'s constructor args (secondary/foreground/
    # background/surface/panel) plus step 7's "no muted grey below 50% luminance" for the two
    # tokens (`dim`, `muted`) that read as low-contrast captions elsewhere.
    "high-contrast": _with_overrides({
        "chrome_dim": "#FFFFFF",
        "dim": "#FFFFFF",
        "muted": "#CCCCCC",
        "foreground": "#FFFFFF",
        "background": "#000000",
        "surface": "#0A0A0A",
        "panel": "#161616",
    }),
    # Deltas match `theme.make_light_theme()`'s constructor args (primary/secondary/foreground/
    # background/surface/panel) plus the chrome-layer extras (`zebra`, `cursor_band`,
    # `gauge_track`, `pill`) recoloured for a light ground -- the dark-ground originals would be
    # nearly invisible on `#F5F5F0`.
    "light": _with_overrides({
        "chrome": "#0E7490",
        "chrome_dim": "#7FB3C2",
        "foreground": "#101214",
        "background": "#F5F5F0",
        "surface": "#FFFFFF",
        "panel": "#E7E7E1",
        "zebra": "#ECECE6",
        "cursor_band": "#D8ECEF",
        "gauge_track": "#DADAD3",
        "pill": "#E2E2DC",
    }),
}

# `dark=` for `textual.theme.Theme` -- fixed per preset; an override never flips this (a light
# preset with every token overridden black would just be a confusing light theme, not a dark one).
PRESET_DARK: dict[str, bool] = {"pantheon": True, "high-contrast": True, "light": False}

PRESET_NAMES: tuple[str, ...] = tuple(PRESETS.keys())


def resolved_tokens(preset: str, overrides: Optional[dict] = None) -> dict:
    """Every token, preset value first, then `overrides` layered on top. Raises
    `UnknownPresetError`/`UnknownTokenError` rather than silently dropping a bad name."""
    if preset not in PRESETS:
        raise UnknownPresetError(preset, PRESET_NAMES)
    tokens = dict(PRESETS[preset])
    for key in overrides or {}:
        if key not in TOKEN_NAMES:
            raise UnknownTokenError(key)
    tokens.update(overrides or {})
    return tokens


def make_theme(name: str, tokens: dict, dark: bool = True):
    """A `textual.theme.Theme` built from a resolved token dict -- imported lazily so tests need
    no terminal, same as `theme.make_textual_theme()`."""
    from textual.theme import Theme

    return Theme(
        name=name,
        primary=tokens["chrome"],
        secondary=tokens["chrome_dim"],
        accent=tokens["chrome"],
        success=tokens["success"],
        warning=tokens["warning"],
        error=tokens["error"],
        foreground=tokens["foreground"],
        background=tokens["background"],
        surface=tokens["surface"],
        panel=tokens["panel"],
        dark=dark,
    )


# ---- pantheon.toml [theme] persistence ---------------------------------------------------------
# No TOML writer library ships in this venv beyond read-only `tomllib`, and `pantheon.toml` is
# hand-maintained (comments, chosen key order) the same way Windows Terminal's file is -- so this
# is a small surgical line-patcher, not a full parse-and-dump: it only ever touches the `preset =`
# line inside `[theme]` and the individual `token = "value"` lines inside `[theme.overrides]`,
# creating either table (or the file itself) when missing, and leaving every other line alone.

_HEADER_RE_TEMPLATE = r'^\[{name}\](?:[ \t]*#.*)?$'


def _table_span(text: str, table: str) -> Optional[tuple[int, int, int]]:
    """`(header_start, content_start, content_end)` for `[table]`; `None` if it does not exist.
    `content_end` stops at the next top-level-looking `[...]` header or EOF."""
    pat = re.compile(_HEADER_RE_TEMPLATE.format(name=re.escape(table)), re.M)
    m = pat.search(text)
    if not m:
        return None
    header_end = m.end()
    content_start = header_end + 1 if text[header_end:header_end + 1] == "\n" else header_end
    next_header = re.search(r"^\[", text[content_start:], re.M)
    content_end = content_start + next_header.start() if next_header else len(text)
    return m.start(), content_start, content_end


def _set_scalar_line(text: str, content_start: int, content_end: int, key: str, literal: str) -> str:
    """Set (or append) one `key = literal` line within `[content_start, content_end)`, keeping a
    same-line trailing `# comment` if there was one."""
    body = text[content_start:content_end]
    pat = re.compile(r"^(" + re.escape(key) + r")([ \t]*=[ \t]*)([^\n#]*?)([ \t]*(#.*)?)$", re.M)
    m = pat.search(body)
    if m:
        line_start = content_start + m.start()
        line_end = content_start + m.end()
        new_line = f"{key}{m.group(2)}{literal}{m.group(4)}"
        return text[:line_start] + new_line + text[line_end:]
    insertion = f"{key} = {literal}\n"
    return text[:content_end] + insertion + text[content_end:]


def _remove_scalar_line(text: str, content_start: int, content_end: int, key: str) -> str:
    body = text[content_start:content_end]
    pat = re.compile(r"^" + re.escape(key) + r"[ \t]*=.*\n?", re.M)
    new_body = pat.sub("", body, count=1)
    return text[:content_start] + new_body + text[content_end:]


def _ensure_table(text: str, table: str) -> tuple[str, tuple[int, int, int]]:
    span = _table_span(text, table)
    if span is not None:
        return text, span
    sep = "" if text.endswith("\n") or not text else "\n"
    text = text + sep + f"\n[{table}]\n"
    span = _table_span(text, table)
    assert span is not None
    return text, span


def load_theme_toml(path: str | Path) -> "config_mod.ThemeOverrides":
    from .. import config as config_mod

    p = Path(path)
    if not p.exists():
        return config_mod.ThemeOverrides()
    import tomllib

    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    return config_mod.ThemeOverrides.from_dict(data.get("theme", {}))


def apply_preset(path: str | Path, name: str) -> dict:
    """Sets `[theme] preset = name` (validated against `PRESET_NAMES`). Existing overrides are
    left alone -- they layer on top of whichever preset is active. Returns the newly resolved
    tokens."""
    if name not in PRESETS:
        raise UnknownPresetError(name, PRESET_NAMES)
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    text, (_, c_start, c_end) = _ensure_table(text, "theme")
    text = _set_scalar_line(text, c_start, c_end, "preset", f'"{name}"')
    p.write_text(text, encoding="utf-8")
    current = load_theme_toml(p)
    return resolved_tokens(current.preset, current.overrides)


def override(path: str | Path, token: str, value: str) -> dict:
    """Sets one `[theme.overrides]` entry. Refused with the full token list if `token` is not one
    `theme.py` defines."""
    if token not in TOKEN_NAMES:
        raise UnknownTokenError(token)
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    text, _ = _ensure_table(text, "theme")
    text, (_, c_start, c_end) = _ensure_table(text, "theme.overrides")
    text = _set_scalar_line(text, c_start, c_end, token, f'"{value}"')
    p.write_text(text, encoding="utf-8")
    current = load_theme_toml(p)
    return resolved_tokens(current.preset, current.overrides)


def reset_token(path: str | Path, token: str) -> dict:
    """Removes one `[theme.overrides]` entry, falling back to whatever the active preset says."""
    if token not in TOKEN_NAMES:
        raise UnknownTokenError(token)
    p = Path(path)
    if not p.exists():
        current = load_theme_toml(p)
        return resolved_tokens(current.preset, current.overrides)
    text = p.read_text(encoding="utf-8")
    span = _table_span(text, "theme.overrides")
    if span is not None:
        _, c_start, c_end = span
        text = _remove_scalar_line(text, c_start, c_end, token)
        p.write_text(text, encoding="utf-8")
    current = load_theme_toml(p)
    return resolved_tokens(current.preset, current.overrides)


def reset_all(path: str | Path) -> dict:
    """Clears every `[theme.overrides]` entry, keeping the active preset."""
    p = Path(path)
    if not p.exists():
        current = load_theme_toml(p)
        return resolved_tokens(current.preset, current.overrides)
    text = p.read_text(encoding="utf-8")
    span = _table_span(text, "theme.overrides")
    if span is not None:
        header_start, c_start, c_end = span
        # Drop the whole `[theme.overrides]` block (header included) -- nothing left to keep.
        text = text[:header_start] + text[c_end:]
        p.write_text(text, encoding="utf-8")
    current = load_theme_toml(p)
    return resolved_tokens(current.preset, current.overrides)


def write_appearance_font(path: str | Path, font: dict) -> None:
    """remember the last-applied font in `[appearance]` (`font_face`, `font_size`,
    `cell_height`, `cell_width`) so `pantheon --settings` shows it without re-reading Windows
    Terminal every time. `font` keys are the `Appearance` dataclass field names (not WT's
    `cellHeight`/`cellWidth` spelling)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    text, (_, c_start, c_end) = _ensure_table(text, "appearance")
    for key, value in font.items():
        literal = f'"{value}"' if isinstance(value, str) else repr(float(value))
        text = _set_scalar_line(text, c_start, c_end, key, literal)
        span = _table_span(text, "appearance")
        assert span is not None
        _, c_start, c_end = span
    p.write_text(text, encoding="utf-8")
