"""Runs one headless local-model job inside a tmux window and reports it like an agent,
the same event/log shape as `pantheon.dispatch.codex_job`:

    python -m pantheon.dispatch.ollama_job --briefing <file> --project-dir <dir> --job-id <id> \
        --runner qwen|goose

Append `queued`; run the runner with the briefing on stdin; append `running`; write everything to
`state/dispatch/<job_id>.log`; append `done` with the exit code. `pantheon/providers/ollama.py`
only calls this once its three readiness gates pass (Ollama reachable, a model pulled, the runner
on PATH), so by the time this module runs, the command below should exist -- but neither runner is
installed on this PC as of 2026-09-26 (see providers/ollama.py's docstring), so `runner_argv`
below is a best-effort reading of each project's own docs, NOT verified against a real run:
  - Qwen Code CLI documents a headless stdin mode with `--input-format`
; `--yolo` skips the interactive
    per-tool confirmation prompt that would otherwise hang a headless run.
  - Goose's exact non-interactive flag was not pinned down in that sweep; `goose run -t -` (a text
    prompt read from stdin) is this adapter's best guess. Confirm against `goose run --help` once
    The user has it installed, and fix this if it's wrong.
Unlike codex_job, this does not live-follow the log into the window while the job runs (simpler
synchronous shape, matching that there is nothing yet to test the streaming path against)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .. import config as config_mod
from .. import events as events_mod
from ..models import Event, utcnow_iso

RUNNERS = ("qwen", "goose")


def runner_argv(runner: str) -> list[str]:
    """Command line for one headless turn, reading the briefing text on stdin."""
    if runner == "qwen":
        return ["qwen", "--input-format", "text", "--yolo"]
    if runner == "goose":
        return ["goose", "run", "-t", "-"]
    raise ValueError(f"unknown local-model runner '{runner}'; expected one of {RUNNERS}")


def _event(cfg, name: str, job_id: str, project_dir: str, detail: str = "", tracker_id: str = "") -> None:
    project = Path(project_dir).name
    extra = {"tracker_id": tracker_id} if tracker_id else {}
    try:
        events_mod.append_event(cfg.events_file, Event(
            ts=utcnow_iso(), event=name, source="ollama", job_id=job_id, cwd=project_dir,
            project=project, detail=detail or None, tmux_pane=os.environ.get("TMUX_PANE"), extra=extra,
        ))
    except OSError:
        pass


def run(cfg, briefing: str, project_dir: str, job_id: str, runner: str, tracker_id: str = "",
        wait_for_enter: bool = True) -> int:
    log_path = Path(cfg.dispatch_dir) / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    text = Path(briefing).read_text(encoding="utf-8")
    _event(cfg, "queued", job_id, project_dir, tracker_id=tracker_id)
    print(f"pantheon: {runner} job {job_id} in {project_dir}")
    print(f"pantheon: log at {log_path}")
    print("-" * 60)
    argv = runner_argv(runner)
    code = 1
    try:
        _event(cfg, "running", job_id, project_dir, tracker_id=tracker_id)
        with open(log_path, "w", encoding="utf-8", newline="\n") as log:
            proc = subprocess.run(argv, input=text, stdout=log, stderr=subprocess.STDOUT,
                                   cwd=project_dir, text=True)
        code = proc.returncode
    except OSError as exc:
        message = f"could not start {runner} ({exc.strerror or exc}); is it on PATH?"
        print(message)
        _event(cfg, "failed", job_id, project_dir, detail=message, tracker_id=tracker_id)
        code = 127
    else:
        _event(cfg, "done", job_id, project_dir, detail=f"exit code {code}", tracker_id=tracker_id)
    print("-" * 60)
    print(f"pantheon: {runner} finished with exit code {code}" + ("" if code == 0 else " (that is a failure)"))
    if wait_for_enter and sys.stdin.isatty():
        try:
            input("press Enter to close this window")
        except EOFError:
            pass
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run one headless local-model job and report it to the deck")
    ap.add_argument("--briefing", required=True)
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--runner", required=True, choices=list(RUNNERS))
    ap.add_argument("--tracker-id", default="")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for Enter at the end")
    args = ap.parse_args(argv)
    cfg = config_mod.load()
    return run(cfg, args.briefing, args.project_dir, args.job_id, args.runner, args.tracker_id,
               wait_for_enter=not args.no_wait)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
