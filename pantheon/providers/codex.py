"""Codex CLI adapter. Codex in v1 is a headless job lane (`codex exec`,
which works from MSYS2 on this box) plus a launcher that opens interactive Codex in a native
Windows console window. Never an interactive Codex pane inside MSYS2 tmux -- OpenAI issue #6994
closed that door ("unlikely to ever be a high enough priority", closed not-planned).

`launch(interactive=False)` =: a tmux window named `codex:<project>` runs
`pantheon.dispatch.codex_job`, which wraps `codex exec` and writes the job's events.
`launch(interactive=True)` = Windows Terminal (or pwsh) on the PC, not in tmux.
Agent rows come from `supervisor.state.fold` and usage from `hud.sources` directly -- this
adapter only launches.
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path

from .. import tmuxctl
from ..config import PROJECT_ROOT
from ..dispatch import briefing as briefing_mod
from ..models import LaunchResult

NAME = "codex"
CAPABILITIES: set[str] = set()  # never interactive_tmux; no other flag is production-read


def job_id_for(project_dir: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"codex-{now.strftime('%Y%m%dT%H%M%SZ')}-{briefing_mod.slug(Path(project_dir).name, 20)}"


class CodexProvider:
    name = NAME

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def capabilities(self) -> set[str]:
        return set(CAPABILITIES)

    # ------------------------------------------------------------------ launch

    def launch(self, project_dir: str, briefing_path: str, interactive: bool = False,
               options: Optional[dict] = None) -> LaunchResult:
        """`options`: `model`, `effort`, `mode` (the sandbox) from the launch-options screen;
        each becomes a `codex` flag (`-m`, `-c model_reasoning_effort=`, `-s`) when set."""
        options = {k: v for k, v in (options or {}).items() if v}
        if interactive:
            return self._launch_native_window(project_dir, options)
        session, tmux = self.cfg.tmux_session, self.cfg.tools.tmux
        job_id = job_id_for(project_dir)
        name = f"codex:{Path(project_dir).name[:10]}"
        py = self.cfg.tools.python
        flags = ""
        if options.get("model"):
            flags += f' --model "{options["model"]}"'
        if options.get("effort"):
            flags += f' --effort "{options["effort"]}"'
        if options.get("mode"):
            flags += f' --sandbox "{options["mode"]}"'
        command = (
            f'cd "{PROJECT_ROOT.as_posix()}" && "{py}" -m pantheon.dispatch.codex_job '
            f'--briefing "{briefing_path}" --project-dir "{project_dir}" --job-id "{job_id}"{flags}'
        )
        index = tmuxctl.new_window(session, name, PROJECT_ROOT.as_posix(), command=command, tmux=tmux, detached=True)
        if index is None:
            return LaunchResult(False, message="tmux would not open a window for the Codex job")
        target = f"{session}:{index}"
        return LaunchResult(True, "headless", index, name, job_id=job_id,
                            tmux_pane=tmuxctl.pane_id(target, tmux),
                            message=f"Codex is working the job in window {index}; j watches it, l shows the log")

    def _launch_native_window(self, project_dir: str, options: Optional[dict] = None) -> LaunchResult:
        tools = self.cfg.tools
        win_dir = project_dir.replace("/", "\\")
        options = options or {}
        codex_cmd = "codex"
        if options.get("model"):
            codex_cmd += f" -m {options['model']}"
        if options.get("effort"):
            codex_cmd += f" -c model_reasoning_effort={options['effort']}"
        if options.get("mode"):
            codex_cmd += f" -s {options['mode']}"
        if os.path.exists(tools.wt):
            argv = [tools.cmd, "/c", "start", "", tools.wt, "-d", win_dir, "pwsh", "-NoExit", "-Command", codex_cmd]
        else:  # no Windows Terminal: a plain PowerShell window does the same job
            argv = [tools.cmd, "/c", "start", "", tools.pwsh, "-NoExit", "-Command",
                    f"Set-Location '{win_dir}'; {codex_cmd}"]
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return LaunchResult(False, "pc-window", message=f"could not open the Codex window: {exc.strerror}")
        return LaunchResult(True, "pc-window",
                            message="Codex opened in a window on the PC, so it will not show on the phone")
