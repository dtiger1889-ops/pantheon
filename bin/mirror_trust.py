"""Mirror Claude Code's per-folder trust answers to the spelling the tmux command line uses.

Claude Code files "Is this a project you trust?" answers in ~/.claude.json under the folder's
spelling. The Desktop app spells folders with backslashes; the MSYS2/tmux command line spells
them with forward slashes, so the same folder is asked again the first time the deck
dispatches into it -- and a typed briefing would answer "No, exit". This adds a forward-slash
twin for every trusted backslash key that lacks one (only the trust flag and the allowed-tools
list are copied; nothing else). It never removes or changes an existing entry, and it writes a
dated backup first.

    python bin/mirror_trust.py            # do it
    python bin/mirror_trust.py --dry-run  # only say what would change
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime

BACKSLASH = chr(92)
PATH = os.path.join(os.path.expanduser("~"), ".claude.json")


def twin(key: str) -> str:
    return key.replace(BACKSLASH, "/")


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    with open(PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    projects = data.setdefault("projects", {})
    added = []
    for key, entry in list(projects.items()):
        if BACKSLASH not in key or not isinstance(entry, dict) or not entry.get("hasTrustDialogAccepted"):
            continue
        t = twin(key)
        if t in projects:
            if not projects[t].get("hasTrustDialogAccepted"):
                projects[t]["hasTrustDialogAccepted"] = True
                added.append(t + "  (trust flag set on existing twin)")
            continue
        projects[t] = {
            "allowedTools": list(entry.get("allowedTools") or []),
            "hasTrustDialogAccepted": True,
        }
        added.append(t)
    for line in added:
        print(("would add " if dry else "added ") + line)
    if not added:
        print("nothing to mirror; every trusted folder already has its forward-slash twin")
        return 0
    if dry:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    backup = PATH + f".bak-mirror-{stamp}"
    shutil.copy2(PATH, backup)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, PATH)
    print(f"{len(added)} mirrored; backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
