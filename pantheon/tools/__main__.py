"""`python -m pantheon.tools` -- the standalone `pantheon --tools` window."""
from __future__ import annotations

from .. import orphan
from .app import main

# `exit_hard`, not `sys.exit`: a driver thread blocked on a dead pty kept the 07:51 apps alive
# after a tmux kill on 2026-09-02 (pantheon/orphan.py, and every other `__main__.py` here).
if __name__ == "__main__":
    orphan.exit_hard(main() or 0)
