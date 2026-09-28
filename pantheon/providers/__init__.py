"""Provider registry. Adapters register in `CONSTRUCTORS`; unknown names fail loudly at
startup with the allowed list."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .claude import ClaudeProvider
from .codex import CodexProvider
from .ollama import OllamaProvider

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .base import Provider

CONSTRUCTORS = {"claude": ClaudeProvider, "codex": CodexProvider, "ollama": OllamaProvider}
KNOWN = tuple(CONSTRUCTORS)


def get_providers(cfg: "Config") -> dict[str, "Provider"]:
    out: dict[str, "Provider"] = {}
    for name in cfg.enabled_providers():
        try:
            ctor = CONSTRUCTORS[name]
        except KeyError:
            raise ValueError(f"unknown provider '{name}' in pantheon.toml; allowed: {', '.join(KNOWN)}") from None
        out[name] = ctor(cfg)
    return out
