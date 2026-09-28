"""Task sources: where the queue's rows come from. The Obsidian Sprints Base is the first
implementation; other Bases, other folders, or a standalone tracker are new files here.
Sources register in `CONSTRUCTORS`; unknown names fail loudly at startup with the allowed list."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .obsidian_base import ObsidianBaseSource
from .standalone import StandaloneSource

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .base import TaskSource

CONSTRUCTORS = {"obsidian_base": ObsidianBaseSource, "standalone": StandaloneSource}
KNOWN = tuple(CONSTRUCTORS)


def get_task_source(cfg: "Config", kind: str | None = None) -> "TaskSource":
    name = kind or cfg.task_source
    try:
        ctor = CONSTRUCTORS[name]
    except KeyError:
        raise ValueError(f"unknown task_source '{name}' in pantheon.toml; allowed: {', '.join(KNOWN)}") from None
    return ctor(cfg)
