"""`python -m pantheon.planning` -- the planning page as its own window."""
from __future__ import annotations

from .. import orphan
from .app import main

if __name__ == "__main__":
    orphan.exit_hard(main() or 0)
