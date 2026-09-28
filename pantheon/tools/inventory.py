"""tools page readers: pure functions over paths passed
in, so tests inject fixtures and no path here is ever hardcoded -- the caller (`app.py`,
`bin/tools_show`) supplies real paths from `config.py`'s `ToolsPage`.

Hard rule: NEVER read or print an auth
file or a token. `codex_config` returns only `model`, `sandbox_mode`, and MCP server NAMES
(the TOML table keys) -- never a server's own args/env, which is where a token would live.
Nothing here ever opens `auth.json`.
"""
from __future__ import annotations

import json
import os
import tomllib as _toml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import frontmatter


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str
    target: Optional[str] = None  # a Codex entry's raw symlink target; None for a Claude skill


@dataclass(frozen=True)
class Hook:
    event: str
    matcher: str
    command: str


@dataclass(frozen=True)
class Guards:
    allow: list
    deny: list


# --------------------------------------------------------------------------- skills

def _read_skill_md(skill_md: Path, fallback_name: str) -> tuple[str, str]:
    """`(name, description)` from a `SKILL.md`'s frontmatter, falling back to the directory
    name and a plain marker when the file is missing or will not parse -- never an exception,
    since a single bad skill directory must not blank the whole page."""
    if not skill_md.is_file():
        return fallback_name, "(no SKILL.md)"
    try:
        post = frontmatter.loads(skill_md.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return fallback_name, "(could not read SKILL.md)"
    name = str(post.get("name") or fallback_name)
    description = str(post.get("description") or "(no description)")
    return name, description


def claude_skills(skills_dir) -> list[Skill]:
    """One entry per subdirectory of `~/.claude/skills/` -- a dir with no `SKILL.md` is listed
    with `description = "(no SKILL.md)"` rather than skipped, so a half-built skill still shows
    up (this page never hides what is really on disk)."""
    base = Path(skills_dir)
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        name, description = _read_skill_md(entry / "SKILL.md", entry.name)
        out.append(Skill(name=name, description=description, path=str(entry)))
    return out


def codex_skills(agents_skills_dir) -> list[Skill]:
    """One entry per item in `~/.agents/skills/` (a git-backed folder of symlinks into
    `~/.claude/skills/`). A symlink whose target is missing
    is listed with `description = "(link broken)"` and `target` set to the raw (unresolved)
    link text, so `sync_state` can mark it `link broken`
    rather than silently calling it in sync."""
    base = Path(agents_skills_dir)
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        try:
            is_link = entry.is_symlink()
        except OSError:
            continue
        target = None
        broken = False
        if is_link:
            try:
                target = os.readlink(entry)
            except OSError:
                target = "?"
            broken = not entry.exists()  # exists() follows the link; False means it dangles
        elif not entry.is_dir():
            continue
        if broken:
            out.append(Skill(name=entry.name, description="(link broken)", path=str(entry), target=target))
            continue
        _, description = _read_skill_md(entry / "SKILL.md", entry.name)
        out.append(Skill(name=entry.name, description=description, path=str(entry), target=target))
    return out


def sync_state(claude: list[Skill], codex: list[Skill]) -> dict[str, str]:
    """`name -> "in sync" | "claude only" | "codex only" | "link broken"`. Matched by directory
    name (the sharing arrangement is a symlink named after the skill), never by the frontmatter
    `name:` field, which can drift from the folder name."""
    claude_names = {s.name for s in claude}
    codex_by_name = {s.name: s for s in codex}
    state: dict[str, str] = {}
    for s in claude:
        entry = codex_by_name.get(s.name)
        if entry is None:
            state[s.name] = "claude only"
        elif entry.description == "(link broken)":
            state[s.name] = "link broken"
        else:
            state[s.name] = "in sync"
    for name in codex_by_name:
        if name not in claude_names:
            state[name] = "codex only"
    return state


# --------------------------------------------------------------------------- hooks + guards

def _read_json(path) -> Optional[dict]:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def _flatten_hooks(data: Optional[dict]) -> list[Hook]:
    """Both `~/.claude/settings.json`'s `hooks` key and `~/.codex/hooks.json`'s own `hooks`
    key share this shape: `{event: [{matcher, hooks: [{type, command}, ...]}, ...]}`."""
    out: list[Hook] = []
    hooks = (data or {}).get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return out
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = str(entry.get("matcher") or "")
            for h in entry.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.append(Hook(event=str(event), matcher=matcher, command=str(h["command"])))
    return out


def claude_hooks(settings_json) -> list[Hook]:
    return _flatten_hooks(_read_json(settings_json))


def codex_hooks(hooks_json) -> list[Hook]:
    return _flatten_hooks(_read_json(hooks_json))


def claude_guards(settings_json) -> Guards:
    """`permissions.allow` / `permissions.deny` from `~/.claude/settings.json` -- the deny list
    is what the spec calls Claude's "guards". Missing file or key -> empty lists, never an
    exception."""
    data = _read_json(settings_json) or {}
    perms = data.get("permissions") if isinstance(data, dict) else None
    perms = perms if isinstance(perms, dict) else {}
    allow = [str(x) for x in (perms.get("allow") or []) if isinstance(x, str)]
    deny = [str(x) for x in (perms.get("deny") or []) if isinstance(x, str)]
    return Guards(allow=allow, deny=deny)


def codex_config(config_toml) -> dict:
    """`{"model", "sandbox_mode", "mcp_servers"}` from `~/.codex/config.toml` -- `mcp_servers`
    is a sorted list of table-key NAMES only (`[mcp_servers.<name>]`), never that table's own
    `env`/`args` values, which is where a token would live. Missing/unparsable file -> the same
    shape with empty/`None` values, never an exception."""
    out: dict = {"model": None, "sandbox_mode": None, "mcp_servers": []}
    p = Path(config_toml)
    if not p.is_file():
        return out
    try:
        with open(p, "rb") as fh:
            data = _toml.load(fh)
    except (OSError, _toml.TOMLDecodeError):
        return out
    out["model"] = data.get("model")
    out["sandbox_mode"] = data.get("sandbox_mode")
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        out["mcp_servers"] = sorted(servers.keys())
    return out
