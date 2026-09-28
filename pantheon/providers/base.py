"""One interface for every model vendor: launch / capabilities. A new vendor is a new file in
this package that exposes `make(cfg) -> Provider`.

The deck never calls `observe`/`usage` on a provider -- agent rows come from
`supervisor.state.fold` and usage from `hud.sources` directly, so the protocol only carries what
production actually calls. The same is true of capability flags: dispatch/
launch.py checks membership of exactly one flag, `interactive_tmux`, so that is the only flag this
package still enumerates."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import LaunchResult


@runtime_checkable
class Provider(Protocol):
    name: str

    def launch(self, project_dir: str, briefing_path: str, interactive: bool) -> LaunchResult: ...

    def capabilities(self) -> set[str]: ...
    # e.g. {"interactive_tmux"} -- dispatch/launch.py checks membership of that one flag
