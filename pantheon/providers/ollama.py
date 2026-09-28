"""Ollama / local-model adapter. Headless only -- capabilities is always empty, same as Codex: nothing about
a local model needs an interactive tmux pane, and C19 already trimmed every capability flag
production doesn't read down to just `interactive_tmux`.

Three-gate readiness, each reported as one plain line, never a traceback:
  1. Ollama itself must be reachable (`curl http://localhost:11434/api/tags`). Checked fresh
     2026-09-26 for this build: NOT installed on this PC -- no
     `AppData/Local/Programs/Ollama/ollama.exe`, no process, no listener on 11434. The user installs/starts it himself; this adapter never
     does either.
  2. At least one model must be pulled.
     Multi-gigabyte pulls are the user's call, never automatic.
  3. A runner that already speaks Ollama with no glue must be on PATH: Qwen Code CLI (`qwen`,
     preferred -- documented headless stdin mode + a hooks system shaped like Claude Code's) or
     Goose (`goose`, the best-documented MSYS2/Git-Bash story). Neither is installed either, and
     this adapter never installs one -- see pantheon/dispatch/ollama_job.py's docstring for the
     command lines, which are a best-effort guess pending a real install to test against.

When all three gates pass, `launch` opens a tmux window and runs `pantheon.dispatch.ollama_job`
in it, the same shape as the Codex headless job, so a local-model job shows up in the deck's rows the same way."""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import tmuxctl
from ..config import PROJECT_ROOT
from ..dispatch import briefing as briefing_mod
from ..models import LaunchResult

NAME = "ollama"
CAPABILITIES: set[str] = set()  # never interactive_tmux -- no local-model pane design exists
TAGS_URL = "http://localhost:11434/api/tags"
PULL_HINT = "ollama pull qwen2.5-coder:7b"
RUNNERS = ("qwen", "goose")  # checked on PATH in this order; qwen preferred per 


def list_models(url: str = TAGS_URL, timeout: float = 1.5) -> tuple[list[str], str]:
    """(model names, note). The note is `ollama not running` when nothing answers; never an error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return [], "ollama not running"
    names = [str(m.get("name") or "") for m in (data.get("models") or []) if isinstance(m, dict)]
    return [n for n in names if n], ""


def find_runner(which: Callable[[str], Optional[str]] = shutil.which) -> str:
    """The first of RUNNERS found on PATH, or "" when neither is installed."""
    for name in RUNNERS:
        if which(name):
            return name
    return ""


def readiness(tags_url: str = TAGS_URL, which: Callable[[str], Optional[str]] = shutil.which) -> tuple[bool, str]:
    """(ready, message). `message` is "" when ready, else the one plain line to show the user --
    never an exception, so the new-session picker can show it inline instead of a traceback."""
    models, note = list_models(tags_url)
    if note:
        return False, f"{note}: install/start Ollama, then `{PULL_HINT}`"
    if not models:
        return False, f"no local model pulled yet: run {PULL_HINT}"
    if not find_runner(which):
        return False, "no local runner found: install Qwen Code CLI (`qwen`) or Goose (`goose`) to drive it"
    return True, ""


def job_id_for(project_dir: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"ollama-{now.strftime('%Y%m%dT%H%M%SZ')}-{briefing_mod.slug(Path(project_dir).name, 20)}"


class OllamaProvider:
    name = NAME

    def __init__(self, cfg, which: Callable[[str], Optional[str]] = shutil.which) -> None:
        self.cfg = cfg
        self._which = which

    def capabilities(self) -> set[str]:
        return set(CAPABILITIES)

    def launch(self, project_dir: str, briefing_path: str, interactive: bool = False) -> LaunchResult:
        ready, message = readiness(which=self._which)
        if not ready:
            return LaunchResult(False, "headless", message=message)
        runner = find_runner(self._which)
        session, tmux = self.cfg.tmux_session, self.cfg.tools.tmux
        job_id = job_id_for(project_dir)
        name = f"ollama:{Path(project_dir).name[:10]}"
        py = self.cfg.tools.python
        command = (
            f'cd "{PROJECT_ROOT.as_posix()}" && "{py}" -m pantheon.dispatch.ollama_job '
            f'--briefing "{briefing_path}" --project-dir "{project_dir}" --job-id "{job_id}" --runner "{runner}"'
        )
        index = tmuxctl.new_window(session, name, PROJECT_ROOT.as_posix(), command=command, tmux=tmux, detached=True)
        if index is None:
            return LaunchResult(False, message="tmux would not open a window for the local-model job")
        target = f"{session}:{index}"
        return LaunchResult(True, "headless", index, name, job_id=job_id,
                            tmux_pane=tmuxctl.pane_id(target, tmux),
                            message=f"{runner} is working the job in window {index}; j watches it, l shows the log")


def make(cfg) -> OllamaProvider:
    return OllamaProvider(cfg)
