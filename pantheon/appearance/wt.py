"""write the four font keys into Windows Terminal's own `settings.json`.

A terminal app cannot control the glyphs its host draws with -- but Windows Terminal
is the desk host and it stores font per profile in a JSON file this
process CAN write, re-reading and re-rendering on every save (its own settings UI relies on
this -- no restart needed).

Schema facts this module relies on:
- the file lives at `%LOCALAPPDATA%/Packages/Microsoft.WindowsTerminal*_*/LocalState/settings.json`
  (glob the hash; `Microsoft.WindowsTerminalPreview_*` too, for Preview users);
- `profiles.list` is an array of profile objects matched by `name` (falling back to
  `profiles.defaults` when no profile matches);
- font keys live under a per-profile `font` object: `font.face`, `font.size`, `font.cellHeight`
  ("like CSS line-height"), `font.cellWidth` ("like CSS letter-spacing").
  https://learn.microsoft.com/en-us/windows/terminal/customize-settings/profile-appearance
  https://github.com/microsoft/terminal/blob/main/doc/cascadia/profiles.schema.json

People often hand-maintain this file with comments (JSONC) and a particular key order, so THE WRITE
IS SURGICAL: only the four font keys of the matched profile are touched, everything else in the
file -- comments, order, unrelated keys -- is left byte-for-byte alone. No JSONC library ships in
this venv, so this module never fully parses-and-dumps the
file. Instead: comments are blanked to same-length whitespace (`_blank_comments`) so the result
is plain JSON with IDENTICAL character offsets to the original -- `json.loads` on that blanked
text is used only to read values and to verify a write parsed; the WRITE itself locates each
target span with a brace/string-aware scan over that same blanked text and edits the ORIGINAL
text at the matching offsets, so untouched bytes (comments included) never move.

The
safeguards: backup every time, patch only the four font keys, reparse-or-restore,
and print what changed after every write. Never blocks waiting for a confirmation.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PHONE_MESSAGE = "font is set in Termius on the phone; this changes the desk only"
NOT_FOUND_MESSAGE = (
    "no Windows Terminal settings.json found on this machine -- font is desk-only and this "
    "isn't the desk (or Windows Terminal isn't installed here)"
)
FONT_KEYS = ("face", "size", "cellHeight", "cellWidth")


@dataclass(frozen=True)
class Font:
    """`None` on any field means "leave it alone" (read_font: "not set in the file";
    write_font: "don't touch this key")."""

    face: Optional[str] = None
    size: Optional[float] = None
    cell_height: Optional[float] = None
    cell_width: Optional[float] = None

    def as_wt_keys(self) -> dict:
        out = {}
        if self.face is not None:
            out["face"] = self.face
        if self.size is not None:
            out["size"] = self.size
        if self.cell_height is not None:
            out["cellHeight"] = self.cell_height
        if self.cell_width is not None:
            out["cellWidth"] = self.cell_width
        return out


class WriteError(Exception):
    """Raised when a write could not be verified -- the backup has already been restored."""


# ---- locating the file -----------------------------------------------------------------------

def find_settings(packages_dir: str | Path | None = None) -> Optional[Path]:
    """Newest-by-mtime `settings.json` under
    `%LOCALAPPDATA%/Packages/Microsoft.WindowsTerminal*_*/LocalState/`. `None` off-Windows, with
    no `LOCALAPPDATA`, or when nothing matches -- callers show `NOT_FOUND_MESSAGE`."""
    import os

    if packages_dir is None:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            return None
        packages_dir = Path(local) / "Packages"
    else:
        packages_dir = Path(packages_dir)
    if not packages_dir.is_dir():
        return None
    matches = list(packages_dir.glob("Microsoft.WindowsTerminal*_*/LocalState/settings.json"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


# ---- comment-safe text scanning --------------------------------------------------------------

def _blank_comments(text: str) -> str:
    """Same length as `text`; `//...` and `/*...*/` comments become spaces (newlines kept, so
    line numbers in any later error are unaffected), string literals are left untouched. The
    result is plain JSON with offsets identical to `text`."""
    out = list(text)
    i, n = 0, len(text)
    in_str = False
    escape = False
    while i < n:
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def _blank_strings(text: str) -> str:
    """Same length; string CONTENTS (not the quotes) become `x`, so a naive brace counter run on
    the result cannot be fooled by a literal `{`/`}` inside a string value. Call this on text
    that has already had comments blanked (`_blank_comments`)."""
    out = list(text)
    i, n = 0, len(text)
    in_str = False
    escape = False
    while i < n:
        c = text[i]
        if in_str:
            if escape:
                out[i] = "x"
                escape = False
            elif c == "\\":
                out[i] = "x"
                escape = True
            elif c == '"':
                in_str = False
            else:
                out[i] = "x"
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        i += 1
    return "".join(out)


def _match_forward(structure: str, open_idx: int) -> int:
    """Index of the `}` (or `]`) matching the opener at `open_idx`, scanning `structure` (a
    string/comment-blanked view where only structural brackets remain meaningful)."""
    opener = structure[open_idx]
    closer = "}" if opener == "{" else "]"
    depth = 0
    for i in range(open_idx, len(structure)):
        c = structure[i]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
    raise WriteError("unbalanced brackets in settings.json -- refusing to guess")


def _enclosing_object(structure: str, inside_idx: int) -> tuple[int, int]:
    """Span `(open_idx, close_idx)` of the `{...}` that directly encloses `inside_idx`."""
    depth = 0
    i = inside_idx
    while i >= 0:
        c = structure[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                return i, _match_forward(structure, i)
            depth -= 1
        i -= 1
    raise WriteError("could not find an enclosing object -- refusing to guess")


_VALUE_RE = r'(?:"(?:[^"\\]|\\.)*"|-?[0-9]+(?:\.[0-9]+)?|true|false|null)'


def _find_profile_span(raw: str, nocomment: str, profile_name: str) -> Optional[tuple[int, int]]:
    """The `(open, close)` span of the profile object in `profiles.list` whose `name` (or, when
    no name matches, `commandline`) equals `profile_name`. `None` when nothing matches."""
    structure = _blank_strings(nocomment)
    for key in ("name", "commandline"):
        pat = re.compile(r'"' + key + r'"\s*:\s*"' + re.escape(profile_name) + r'"')
        m = pat.search(nocomment)
        if m:
            return _enclosing_object(structure, m.start())
        # commandline is rarely an exact match -- try "contains" too.
        if key == "commandline":
            pat2 = re.compile(r'"commandline"\s*:\s*"[^"\n]*' + re.escape(profile_name) + r'[^"\n]*"')
            m2 = pat2.search(nocomment)
            if m2:
                return _enclosing_object(structure, m2.start())
    return None


def _find_defaults_span(raw: str, nocomment: str) -> tuple[int, int]:
    structure = _blank_strings(nocomment)
    m = re.search(r'"profiles"\s*:\s*\{', nocomment)
    if not m:
        raise WriteError("no [profiles] object in settings.json")
    prof_open, prof_close = _enclosing_object(structure, m.end() - 1)
    m2 = re.search(r'"defaults"\s*:\s*\{', nocomment[prof_open:prof_close])
    if not m2:
        raise WriteError("no profiles.defaults object in settings.json")
    d_open = prof_open + m2.end() - 1
    return _enclosing_object(structure, d_open)


def _indent_at(raw: str, idx: int) -> str:
    """The whitespace opening the line that `idx` sits on -- used so an inserted key matches its
    neighbours' indentation instead of landing at column 0."""
    line_start = raw.rfind("\n", 0, idx) + 1
    line = raw[line_start:idx]
    m = re.match(r"[ \t]*", line)
    return (m.group(0) if m else "") + "  "


def _json_value_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return json.dumps(value)


def _set_object_key(raw: str, obj_open: int, obj_close: int, key: str, value) -> tuple[str, dict]:
    """Patch (or insert) one `"key": value` pair inside the object spanning `[obj_open, obj_close]`
    of `raw`. Returns the new full text and a `{"action": "changed"|"added", "old": ..., "new": ...}`
    record for the printed changelog. Only this one key is touched."""
    nocomment = _blank_comments(raw)
    body = nocomment[obj_open:obj_close]
    literal = _json_value_literal(value)
    key_re = re.compile(r'"' + re.escape(key) + r'"\s*:\s*(' + _VALUE_RE + r')')
    m = key_re.search(body)
    if m:
        start = obj_open + m.start()
        end = obj_open + m.end()
        old_value = m.group(1)
        new_text = raw[:start] + f'"{key}": {literal}' + raw[end:]
        return new_text, {"action": "changed", "old": old_value, "new": literal}
    # Not present: insert right after the opening brace, before whatever comes next.
    indent = _indent_at(raw, obj_open + 1) if obj_open + 1 < len(raw) else "  "
    insert_at = obj_open + 1
    # Is the object otherwise empty (aside from whitespace/comments)?
    rest = nocomment[obj_open + 1:obj_close].strip()
    if rest:
        insertion = f'\n{indent}"{key}": {literal},'
    else:
        outer_indent = _indent_at(raw, obj_open)
        insertion = f'\n{indent}"{key}": {literal}\n{outer_indent}'
    new_text = raw[:insert_at] + insertion + raw[insert_at:]
    return new_text, {"action": "added", "old": None, "new": literal}


def _ensure_font_object(raw: str, profile_open: int, profile_close: int) -> tuple[str, int, int]:
    """Returns `(text, font_open, font_close)`, creating an empty `"font": {}` inside the profile
    if none exists yet."""
    nocomment = _blank_comments(raw)
    structure = _blank_strings(nocomment)
    m = re.search(r'"font"\s*:\s*\{', nocomment[profile_open:profile_close])
    if m:
        f_open = profile_open + m.end() - 1
        f_close = _match_forward(structure, f_open)
        return raw, f_open, f_close
    indent = _indent_at(raw, profile_open + 1)
    rest = nocomment[profile_open + 1:profile_close].strip()
    insert_at = profile_open + 1
    if rest:
        insertion = f'\n{indent}"font": {{}},'
    else:
        outer_indent = _indent_at(raw, profile_open)
        insertion = f'\n{indent}"font": {{}}\n{outer_indent}'
    new_text = raw[:insert_at] + insertion + raw[insert_at:]
    f_open = insert_at + insertion.index("{")
    f_close = f_open + 1
    return new_text, f_open, f_close


# ---- public read/write ------------------------------------------------------------------------

_KEY_TO_ATTR = {"face": "face", "size": "size", "cellHeight": "cell_height", "cellWidth": "cell_width"}


def read_font(path: str | Path, profile_name: str) -> Font:
    """Current face/size/cellHeight/cellWidth for `profile_name`, falling back to
    `profiles.defaults` when no profile matches by name or commandline."""
    raw = Path(path).read_text(encoding="utf-8")
    nocomment = _blank_comments(raw)
    span = _find_profile_span(raw, nocomment, profile_name)
    if span is None:
        span = _find_defaults_span(raw, nocomment)
    body = nocomment[span[0]:span[1]]
    m = re.search(r'"font"\s*:\s*\{', body)
    values: dict = {}
    if m:
        structure = _blank_strings(nocomment)
        f_open = span[0] + m.end() - 1
        f_close = _match_forward(structure, f_open)
        font_body = nocomment[f_open:f_close]
        for key in FONT_KEYS:
            km = re.search(r'"' + key + r'"\s*:\s*(' + _VALUE_RE + r')', font_body)
            if km:
                values[key] = json.loads(km.group(1))
    return Font(
        face=values.get("face"),
        size=values.get("size"),
        cell_height=values.get("cellHeight"),
        cell_width=values.get("cellWidth"),
    )


def backup_path(path: Path, state_dir: str | Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = Path(state_dir) / "appearance"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"settings.json.{ts}.bak"


def write_font(
    path: str | Path,
    profile_name: str,
    font: Font,
    state_dir: str | Path,
) -> dict:
    """Backs up `path`, patches only the given `font` fields (fields left `None` are untouched)
    on the matched profile (or `profiles.defaults`, with a note), re-parses the result to make
    sure it is still valid JSON and the values took, and RESTORES the backup if not. Returns a
    report dict: `{"backup": Path, "profile": "name"|"defaults", "changes": [...]}`. Prints
    nothing itself -- callers (the screen, `bin/appearance_apply`) print the report."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    backup = backup_path(path, state_dir)
    shutil.copy2(path, backup)

    nocomment = _blank_comments(raw)
    span = _find_profile_span(raw, nocomment, profile_name)
    which = "matched"
    if span is None:
        span = _find_defaults_span(raw, nocomment)
        which = "defaults"

    wanted = font.as_wt_keys()
    if not wanted:
        return {"backup": backup, "profile": which, "changes": []}

    text = raw
    changes = []
    try:
        for key, value in wanted.items():
            nocomment = _blank_comments(text)
            span = _find_profile_span(text, nocomment, profile_name)
            if span is None:
                span = _find_defaults_span(text, nocomment)
            text, f_open, f_close = _ensure_font_object(text, span[0], span[1])
            text, change = _set_object_key(text, f_open, f_close, key, value)
            change["key"] = key
            changes.append(change)

        # Verify: still parses, and the values actually took.
        json.loads(_blank_comments(text))
        tmp = _write_temp(text)
        try:
            readback = read_font(tmp, profile_name)
        finally:
            tmp.unlink(missing_ok=True)
        for key, value in wanted.items():
            got = getattr(readback, _KEY_TO_ATTR[key])
            if got != value:
                raise WriteError(f"wrote {key}={value!r} but read back {got!r}")
    except Exception as exc:
        shutil.copy2(backup, path)
        raise WriteError(f"font write failed and was rolled back from {backup}: {exc}") from exc

    path.write_text(text, encoding="utf-8")
    return {"backup": backup, "profile": which, "changes": changes}


def _write_temp(text: str) -> Path:
    """A throwaway file for `read_font`'s own re-parse of a not-yet-committed edit -- verification
    must exercise the exact same parser/locator path a real read would use, not a shortcut."""
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".json")
    import os as _os

    with _os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return Path(name)
