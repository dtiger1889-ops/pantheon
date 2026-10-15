"""Runs one headless Codex job inside a tmux window and reports it like an agent.

    python -m pantheon.dispatch.codex_job --briefing <file> --project-dir <dir> --job-id <id>

What it does, in order: append `queued`; run `codex exec --skip-git-repo-check -s workspace-write
-C <dir> -` with the briefing on stdin; append `running`; copy every output line to the
screen AND to `state/dispatch/<job_id>.log`; append `done` with the exit code (the supervisor turns
a non-zero code into `failed`). Then it waits for Enter so the output stays readable in the window,
but never past LINGER_SECONDS: it then exits on its own (the log keeps the output). A codex child
still running at the JOB_DEADLINE_SECONDS deadline is stopped (terminate, then kill) and its exit
checked before `done` is written. Both bounds exist because a runner sat at the Enter prompt for 28
hours after its job finished.
Codex's Windows sandbox may ask something on the first run in a new folder; that is the user's
click and it shows in the window, it is never bypassed here.
"""
from __future__ import annotations

import argparse
import os
import select
import subprocess
import sys
import time
from pathlib import Path

from .. import config as config_mod
from .. import events as events_mod
from ..models import Event, utcnow_iso

JOB_DEADLINE_SECONDS = 4 * 60 * 60   # a codex job still running after this is stopped
STOP_GRACE_SECONDS = 10              # terminate, wait this long, then kill, wait again
LINGER_SECONDS = 10 * 60             # how long the finished window waits for Enter
DEADLINE_EXIT_CODE = 124             # the usual "timed out" exit code


def stop_child(proc: subprocess.Popen, grace: float = STOP_GRACE_SECONDS) -> bool:
    """Terminate, then kill; True only once the child has really exited."""
    for send in (proc.terminate, proc.kill):
        if proc.poll() is not None:
            return True
        try:
            send()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
            return True
        except subprocess.TimeoutExpired:
            continue
    return proc.poll() is not None


def wait_for_line(seconds: float, stream=None) -> bool:
    """Wait at most `seconds` for a line on `stream` (default stdin); True if one came."""
    stream = sys.stdin if stream is None else stream
    try:
        ready, _, _ = select.select([stream], [], [], max(0.0, seconds))
    except (OSError, ValueError):  # no selectable stdin: do not wait at all
        return False
    if not ready:
        return False
    try:
        stream.readline()
    except OSError:
        pass
    return True


def _last_say(log_path: Path, limit: int = 120) -> str:
    """The last non-empty, non-boilerplate line of the job log, clipped."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line or line.startswith(("hook:", "tokens used", "EXIT=", "-" * 10)) or line == "codex":
            continue
        if line.replace(",", "").replace(".", "").isdigit():  # the bare count under "tokens used"
            continue
        return line[:limit]
    return ""


def _event(cfg, name: str, job_id: str, project_dir: str, detail: str = "", tracker_id: str = "") -> None:
    project = Path(project_dir).name
    extra = {"tracker_id": tracker_id} if tracker_id else {}
    try:
        events_mod.append_event(cfg.events_file, Event(
            ts=utcnow_iso(), event=name, source="codex", job_id=job_id, cwd=project_dir,
            project=project, detail=detail or None, tmux_pane=os.environ.get("TMUX_PANE"), extra=extra,
        ))
    except OSError:
        pass


def codex_argv(cfg, project_dir: str, model: str = "", effort: str = "", sandbox: str = "") -> list[str]:
    """`codex exec` with the flags that stop it hanging, plus the launch options when given
   ."""
    argv = [cfg.tools.codex, "exec", "--skip-git-repo-check", "-s", sandbox or "workspace-write"]
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    argv += ["-C", project_dir, "-"]
    return argv


def run(cfg, briefing: str, project_dir: str, job_id: str, tracker_id: str = "",
        wait_for_enter: bool = True, model: str = "", effort: str = "", sandbox: str = "",
        deadline_seconds: float = JOB_DEADLINE_SECONDS, linger_seconds: float = LINGER_SECONDS,
        stop_grace: float = STOP_GRACE_SECONDS) -> int:
    log_path = Path(cfg.dispatch_dir) / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    text = Path(briefing).read_text(encoding="utf-8")
    _event(cfg, "queued", job_id, project_dir, tracker_id=tracker_id)
    print(f"pantheon: codex job {job_id} in {project_dir}")
    print(f"pantheon: log at {log_path}")
    print("-" * 60)
    # The plain two-argument form when no launch option is set (tests replace `codex_argv`).
    argv = (codex_argv(cfg, project_dir, model, effort, sandbox) if (model or effort or sandbox)
            else codex_argv(cfg, project_dir))
    code = 1
    timed_out, child_exited = False, True
    try:
        # File redirection, not PIPEs. Codex's unified-exec runner failed twice with "timed out
        # connecting runner pipe-in" when codex ran as a piped child of MSYS2 python inside tmux
        #, while the identical command with plain
        # file redirection worked; the controlled A/B repro wedged the tmux server outright, so
        # the deck ships the shape that is known to work and the root cause stays an open thread.
        stdin_path = Path(cfg.dispatch_dir) / f"{job_id}.stdin"
        stdin_path.write_text(text, encoding="utf-8")
        with open(log_path, "w", encoding="utf-8", newline="\n") as log, \
                open(stdin_path, encoding="utf-8") as fin:
            proc = subprocess.Popen(
                argv, stdin=fin, stdout=log, stderr=subprocess.STDOUT, cwd=project_dir,
            )
            _event(cfg, "running", job_id, project_dir, tracker_id=tracker_id)
            # The window still shows live output: follow the log file while the job runs.
            pos = 0
            started = time.monotonic()
            while proc.poll() is None:
                if time.monotonic() - started >= deadline_seconds:
                    timed_out = True
                    child_exited = stop_child(proc, stop_grace)
                    break
                time.sleep(0.5)
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
            code = DEADLINE_EXIT_CODE if timed_out else proc.wait()
            with open(log_path, encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                tail_chunk = f.read()
            if tail_chunk:
                sys.stdout.write(tail_chunk)
                sys.stdout.flush()
    except OSError as exc:
        message = f"could not start codex ({exc.strerror or exc}); is the shim at {cfg.tools.codex}?"
        print(message)
        _event(cfg, "failed", job_id, project_dir, detail=message, tracker_id=tracker_id)
        code = 127
    else:
        # Carry the job's FINAL SAY into the done event: Codex exits 0 even when it spent the
        # whole run apologizing, and a bare "exit code 0" presented that as a clean finish. The
        # last non-empty output line is the cheapest honest signal short of understanding the log.
        if timed_out:
            say = f"stopped at the {int(deadline_seconds)}-second deadline" + (
                "" if child_exited else "; codex did not exit after terminate and kill")
            print(f"pantheon: {say}")
        else:
            say = _last_say(log_path)
        _event(cfg, "done", job_id, project_dir,
               detail=f"exit code {code} · {say}".rstrip(" ·"),
               tracker_id=tracker_id)
    print("-" * 60)
    print(f"pantheon: codex finished with exit code {code}" + ("" if code == 0 else " (that is a failure)"))
    if wait_for_enter and sys.stdin.isatty():
        minutes = max(1, round(linger_seconds / 60))
        print(f"press Enter to close this window (it closes by itself in {minutes} min; the log keeps the output)",
              flush=True)
        wait_for_line(linger_seconds)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run one headless Codex job and report it to the deck")
    ap.add_argument("--briefing", required=True)
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--tracker-id", default="")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for Enter at the end")
    ap.add_argument("--model", default="", help="codex -m <model> (else ~/.codex/config.toml)")
    ap.add_argument("--effort", default="", help="model_reasoning_effort: minimal|low|medium|high|xhigh")
    ap.add_argument("--sandbox", default="", help="codex -s: workspace-write|read-only|danger-full-access")
    ap.add_argument("--deadline-seconds", type=float, default=JOB_DEADLINE_SECONDS,
                    help="stop codex if it is still running after this many seconds")
    ap.add_argument("--linger-seconds", type=float, default=LINGER_SECONDS,
                    help="how long the finished window waits for Enter before closing")
    args = ap.parse_args(argv)
    cfg = config_mod.load()
    return run(cfg, args.briefing, args.project_dir, args.job_id, args.tracker_id, wait_for_enter=not args.no_wait,
               model=args.model, effort=args.effort, sandbox=args.sandbox,
               deadline_seconds=args.deadline_seconds, linger_seconds=args.linger_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
