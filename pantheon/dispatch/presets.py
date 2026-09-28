"""Workflow presets: a named bundle of
the launch choices the user keeps making by hand -- which folder, who works it, model, effort,
permission mode, and a first message.

Where they live: `[presets.<name>]` tables in `pantheon.toml`. Every reader goes through `load`, so a second source
is one change in that one function.

    [presets.hikinglog-reader]
    project = "hiking_log_v2"     # optional; leave it out and name the folder on the command line
    who = "claude"                # claude | codex-pc | codex-headless
    model = "sonnet"
    effort = "high"
    mode = "acceptEdits"
    message = "Re-orient from CHECKPOINT.md in {project}, then carry on."

Two entry points use them, no third: `pantheon open --preset NAME` and
`bin/schedule --preset NAME`. A flag typed on the command line always beats the preset.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

FIELDS = ("project", "who", "model", "effort", "mode", "message")


class PresetError(ValueError):
    """A preset that is not there, or not a table. The message is written for the user."""


def _where(cfg) -> str:
    """The file a person would open to fix a preset."""
    from .. import config as config_mod
    return str(config_mod.DEFAULT_TOML).replace("\\", "/")


def load(cfg) -> dict[str, dict]:
    """Every preset by name, each trimmed to the fields a launch understands (anything else in
    the table is ignored, the way `Governor.from_dict` ignores unknown keys)."""
    raw = getattr(cfg, "presets", None) or {}
    out: dict[str, dict] = {}
    for name, table in raw.items():
        if not isinstance(table, dict):
            continue
        out[str(name)] = {k: str(table[k]) for k in FIELDS if table.get(k) not in (None, "")}
    return out


def resolve(name: str, cfg, overrides: Optional[dict] = None) -> dict:
    """The preset `name` with `overrides` laid over it. An override that is `None` or empty
    means "not typed" and leaves the preset's value alone. `{project}` in the message becomes
    the project folder's name once the project is known. Raises `PresetError` for an unknown
    name, naming the file to fix."""
    presets = load(cfg)
    if name not in presets:
        known = ", ".join(sorted(presets)) or "none yet"
        raise PresetError(
            f"there is no preset called '{name}'. Presets are [presets.<name>] tables in "
            f"{_where(cfg)} (the ones there now: {known})."
        )
    merged = dict(presets[name])
    for key, value in (overrides or {}).items():
        if value not in (None, ""):
            merged[key] = value
    project = merged.get("project")
    if merged.get("message") and project:
        merged["message"] = merged["message"].replace("{project}", Path(str(project)).name)
    return merged
