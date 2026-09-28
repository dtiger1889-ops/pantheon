"""`bin/tools_show` -- prints the whole tool inventory as JSON (skills with sync state,
hooks, guards), so it is testable from a script and the user can diff it across machines
. Read-only, same as everything else in this package."""
from __future__ import annotations

import json

from .. import config as config_mod
from .app import build_snapshot


def main() -> int:
    cfg = config_mod.load()
    print(json.dumps(build_snapshot(cfg), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
