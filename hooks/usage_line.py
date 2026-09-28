"""usage_line.py -- Claude Code `UserPromptSubmit` hook: prints ONE usage line that Claude reads
before each prompt (what it says and where each number comes from: `pantheon/hud/prompt_line.py`).

Runs under the MSYS2 venv, which needs MSYS2's own /usr/bin on PATH to load its DLL, so the
registered command goes through MSYS2's env.exe (checked from Git Bash and from PowerShell):

    C:/msys64/usr/bin/env.exe PATH=/usr/bin <checkout>/.venv/bin/python /<drive>/<checkout path>/hooks/usage_line.py

The script path MUST be the /c/... form: MSYS2 Python treats a `C:/...` script argument as
relative and fails with "can't open file '<cwd>/C:/...'".

Finds its own checkout from this file's folder (like `pantheon_event.ps1`). Reads the hook payload
(`session_id`, `transcript_path`) from stdin. Never blocks or fails a prompt: any error prints
nothing and exits 0 (PANTHEON_USAGE_LINE_DEBUG=1 prints the traceback to stderr). Off: `[usage] prompt_line = false` in pantheon.toml, or PANTHEON_USAGE_LINE=0.
"""
import json
import os
import sys


def main() -> None:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        payload = {}
        try:
            if not sys.stdin.isatty():
                data = json.loads(sys.stdin.read() or "{}")
                if isinstance(data, dict):
                    payload = data
        except ValueError:
            pass    # an unreadable payload only costs the session share, not the line
        from pantheon import config as config_mod
        from pantheon.hud import prompt_line
        text = prompt_line.line(config_mod.load(), payload)
        if text:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except BaseException:
        if os.environ.get("PANTHEON_USAGE_LINE_DEBUG"):
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
    os._exit(0)
