"""statusline.py -- Claude Code's statusLine command in ONE process: the Pantheon capture
(`pantheon/hud/capture.py`) plus, inside a Hintforge-format game guide folder (`Guides/<game>/`),
a short guide segment.

Replaces a bash chain (a wrapper script -> bin/statusline_capture -> python) that forked about
eight MSYS2 bash processes per refresh per open session (status line bash was the largest share
of 68 new bash processes in 10 s). Registered like `usage_line.py`, through MSYS2's env.exe so the
venv python finds its DLLs, with the /c/... script path:

    C:/msys64/usr/bin/env.exe PATH=/usr/bin <pantheon folder>/.venv/bin/python /c/<pantheon folder>/hooks/statusline.py

Fail-open: whatever happens, one line goes to stdout and the exit code is 0.
To revert: point settings.json "statusLine".command back at bin/statusline_capture.
"""
import json
import os
import re
import sys

FALLBACK = "pantheon hud: no data"
DOT = "\u00b7"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def guide_segment(cwd: str) -> str:
    """'<persona> . <game> guide . enemy tier N . puzzle tier N' inside Guides/<game>/, else ''."""
    cwd = re.sub(r"/+", "/", (cwd or "").replace("\\", "/"))
    marker = "/Guides/"
    if marker not in cwd:
        return ""
    head, _, rest = cwd.partition(marker)
    game = rest.split("/", 1)[0]
    if not game:
        return ""
    folder = f"{head}{marker}{game}"

    persona = ""
    in_block = False
    for line in _read(f"{folder}/persona.md").splitlines():
        if line.startswith("## Current active persona"):
            in_block = True
            continue
        if in_block and line.startswith("---"):
            break
        if in_block:
            m = re.match(r"\*\*([^*]*)\*\*", line)
            if m:
                persona = m.group(1)
                break

    tiers = _read(f"{folder}/warning_tiers.md")
    en = re.search(r"^- \*\*Enemies: Tier (\d+)\*\*", tiers, re.M)
    pz = re.search(r"^- \*\*Puzzles: Tier (\d+)\*\*", tiers, re.M)

    seg = f"{game.replace('_', ' ')} guide"
    if persona:
        seg = f"{persona} {DOT} {seg}"
    if en:
        seg = f"{seg} {DOT} enemy tier {en.group(1)}"
    if pz:
        seg = f"{seg} {DOT} puzzle tier {pz.group(1)}"
    return seg


def main() -> None:
    line = FALLBACK
    raw = ""
    try:
        raw = sys.stdin.read()
        # Same root contract as bin/statusline_capture: PANTHEON_ROOT tries a worktree's code,
        # and the capture runs from that root so a relative state_dir still lands in Pantheon.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        root = os.environ.get("PANTHEON_ROOT") or here
        os.chdir(root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from pantheon.hud import capture
        line = capture.capture(raw) or FALLBACK
    except BaseException:
        line = FALLBACK
    try:
        data = json.loads(raw) if raw else {}
        cwd = data.get("cwd") or (data.get("workspace") or {}).get("current_dir") or ""
        seg = guide_segment(cwd)
        if seg:
            line = f"{line}   {seg}"
    except BaseException:
        pass
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
    os._exit(0)
