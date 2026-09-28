"""`python -m pantheon.appearance` -- the standalone `pantheon --settings` window."""
from __future__ import annotations

from .. import orphan
from .app import main

# `exit_hard`, not `sys.exit` -- same reason as every other `__main__.py` here
#.
if __name__ == "__main__":
    orphan.exit_hard(main() or 0)
