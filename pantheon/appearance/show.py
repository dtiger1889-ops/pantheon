"""`bin/appearance_show` -- prints the resolved font and active palette as JSON
. Read-only, same discipline as `pantheon.tools.show`."""
from __future__ import annotations

import json

from .. import config as config_mod
from .app import snapshot


def main() -> int:
    cfg = config_mod.load()
    print(json.dumps(snapshot(cfg), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
