"""Slash-command completion source.

Kept as its own importable module -- not folded into `notes/app.py` -- because the spec calls for
it to be reused by the new-session first-message box later.

Three sources, merged and de-duplicated by command name (first source wins: an explicit command
file is more likely current than a guessed built-in):

  1. `<claude_home>/commands/*.md` -- the file stem is the command (`checkpoint.md` -> `/checkpoint`);
     its frontmatter `description:` is the one-line summary, when present.
  2. Skill directories (`<claude_home>/skills/*/` and any `extra_skills_dirs`, e.g. the Codex-shared
     `~/.agents/skills` junction) -- the directory name is the command (`/skill-name`); its
     `SKILL.md` frontmatter `description:` is the summary.
  3. A small static list of Claude Code's own built-ins.

This module never sends anything anywhere -- it only lists and matches. Inserting the chosen text
into a draft, and not sending it, is the caller's job (`notes/app.py`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import frontmatter

# Built-ins as of 2026-09-05 -- verify against `claude --help` if this list starts looking stale.
BUILTIN_COMMANDS = [
    "/model", "/effort", "/compact", "/clear", "/resume", "/checkpoint", "/status", "/help",
]


@dataclass(frozen=True)
class Command:
    name: str              # "/checkpoint" -- always leads with the slash
    description: str = ""
    source: str = ""       # "command" | "skill" | "builtin"


def _description_from_md(path: Path) -> str:
    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # A command/skill file with a description that is not valid YAML (a bare colon, say --
        # seen on a real skill file while building this) must never take the whole scratchpad
        # down; same "never fatal" rule `obsidian_base.py` uses for the exact same reader.
        return ""
    desc = post.metadata.get("description")
    return str(desc).strip() if desc else ""


def _from_commands_dir(commands_dir: Path) -> list[Command]:
    if not commands_dir.is_dir():
        return []
    return [
        Command(f"/{p.stem}", _description_from_md(p), "command")
        for p in sorted(commands_dir.glob("*.md"))
    ]


def _from_skills_dir(skills_dir: Path) -> list[Command]:
    if not skills_dir.is_dir():
        return []
    out = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        desc = _description_from_md(skill_md) if skill_md.is_file() else ""
        out.append(Command(f"/{d.name}", desc, "skill"))
    return out


def _from_builtins() -> list[Command]:
    return [Command(name, "", "builtin") for name in BUILTIN_COMMANDS]


def list_commands(
    claude_home: Optional[str] = None, extra_skills_dirs: Optional[list[str]] = None
) -> list[Command]:
    """Every command this session can name, de-duplicated (first hit of commands, then skills,
    then built-ins, wins). `claude_home` defaults to `~/.claude`."""
    home = Path(claude_home) if claude_home else Path.home() / ".claude"
    found: list[Command] = []
    found += _from_commands_dir(home / "commands")
    found += _from_skills_dir(home / "skills")
    for extra in extra_skills_dirs or []:
        found += _from_skills_dir(Path(extra))
    found += _from_builtins()
    seen: dict[str, Command] = {}
    for cmd in found:
        seen.setdefault(cmd.name, cmd)
    return list(seen.values())


def default_skills_dirs(claude_home: str) -> list[str]:
    """The Codex-shared skills junction alongside `claude_home` (`~/.claude/skills` junctioned into `~/.agents/skills`) -- swept in ADDITION to
    `<claude_home>/skills`, which `list_commands` already covers on its own."""
    return [str(Path(claude_home).parent / ".agents" / "skills")]


def token_at(prefix: str) -> Optional[str]:
    """The `/...` token ending at `prefix` (the text up to the caret, on the current line), or
    `None` if the caret is not on one. A token begins at the start of the line or after
    whitespace -- `str.isspace` is `True` for `"\\n"` too, so passing the WHOLE
    document's text-up-to-caret instead of just the current line also works, harmlessly."""
    i = len(prefix)
    while i > 0 and not prefix[i - 1].isspace():
        i -= 1
    token = prefix[i:]
    return token if token.startswith("/") else None


def match(token: str, commands: list[Command]) -> list[Command]:
    """Commands whose name starts with `token` (leading `/` optional), case-insensitive,
    alphabetical. A bare `/` matches everything."""
    t = token if token.startswith("/") else f"/{token}"
    t = t.lower()
    out = [c for c in commands if c.name.lower().startswith(t)]
    out.sort(key=lambda c: c.name)
    return out
