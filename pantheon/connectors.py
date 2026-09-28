"""The connector gap list: which MCP servers a Claude Code
session in this folder can actually see, read straight off disk -- never a network call.

claude.ai's own connectors (Gmail, Calendar, Drive...) ride along automatically for any session on
the claude.ai subscription login and vanish the instant `ANTHROPIC_API_KEY` is set; Claude
Desktop's own `claude_desktop_config.json` servers have no bridge on native Windows. Both of those
are facts to SHOW, not gaps this module tries to close.
This is a list, not a live check: it reads `.claude.json` and `.mcp.json` as JSON and reports their
`mcpServers` keys, nothing more.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

USER_CLAUDE_JSON = str(Path.home() / ".claude.json")  # per-user; same convention as
# pantheon/dispatch/sessions.py's `claude_home` fallback -- callers with a `Config` can pass
# their own `cfg.claude_home`-derived path instead of this default.

FIXED_SENTENCE = (
    "claude.ai connectors (Gmail, Calendar, Drive...) are shared automatically while every "
    "session is on the claude.ai login -- never set ANTHROPIC_API_KEY for the Claude adapter. "
    "Servers added only to Claude Desktop's own config are not bridged on Windows; add them "
    "with claude mcp add."
)


def _read_json(path) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def servers_from(data: Optional[dict]) -> list[str]:
    """The `mcpServers` keys of one JSON object -- the shape both `.claude.json` (user scope, and
    each entry in its `projects` map) and a project's own `.mcp.json` use."""
    if not isinstance(data, dict):
        return []
    servers = data.get("mcpServers")
    return sorted(servers.keys()) if isinstance(servers, dict) else []


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def project_servers(user_data: Optional[dict], project_dir: str) -> list[str]:
    """The per-project entry inside `.claude.json`'s own `projects` map, if this folder has one."""
    if not isinstance(user_data, dict):
        return []
    projects = user_data.get("projects")
    if not isinstance(projects, dict):
        return []
    target = _norm(project_dir)
    for key, value in projects.items():
        if _norm(str(key)) == target and isinstance(value, dict):
            return servers_from(value)
    return []


def find_mcp_servers(
    project_dir: str, user_json: str = USER_CLAUDE_JSON
) -> tuple[list[str], list[str]]:
    """`(names, problems)`. `problems` names any file that exists but would not parse as JSON --
    reported to the user verbatim (`could not read <path>`), never silently dropped."""
    names: set[str] = set()
    problems: list[str] = []

    if Path(user_json).exists():
        user_data = _read_json(user_json)
        if user_data is None:
            problems.append(f"could not read {user_json}")
        else:
            names.update(servers_from(user_data))
            names.update(project_servers(user_data, project_dir))

    project_mcp = Path(project_dir) / ".mcp.json"
    if project_mcp.exists():
        project_data = _read_json(project_mcp)
        if project_data is None:
            problems.append(f"could not read {project_mcp}")
        else:
            names.update(servers_from(project_data))

    return sorted(names), problems


def report(project_dir: str, user_json: str = USER_CLAUDE_JSON) -> str:
    """Everything the `?` overlay's connectors page and `pantheon --connectors` print."""
    names, problems = find_mcp_servers(project_dir, user_json)
    lines = list(problems)
    lines.append(
        f"MCP servers the CLI sees: {', '.join(names)}" if names
        else "MCP servers the CLI sees: none found"
    )
    lines.append(FIXED_SENTENCE)
    return "\n".join(lines)
