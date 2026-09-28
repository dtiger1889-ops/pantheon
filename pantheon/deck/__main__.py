"""`python -m pantheon.deck` -- tmux window 0."""
from __future__ import annotations

from .. import orphan
from .app import main

# `exit_hard`, not `sys.exit`: a driver thread blocked on a dead pty kept the 07:51 apps alive
# after a tmux kill on 2026-09-02 (pantheon/orphan.py).
if __name__ == "__main__":
    orphan.exit_hard(main())
