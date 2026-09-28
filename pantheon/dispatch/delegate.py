"""Path A/B router: `pantheon delegate "<task>"` -- one command a live Codex session runs to
hand a task to a Claude worker.

When the pantheon tmux (session 0) is reachable (`reach.pantheon_tmux_reachable`), the worker
opens as a new window in it over ssh -- Path A, deck-visible, so THE PIT shows every worker Codex
launches. When it is not, this shells straight to a separate `delegate-claude` helper script (not part of this repo). Same command either way; Codex never has to know which one ran.

Never hangs: the reachability check has its own timeout (`reach.py`), and a Path A launch that
raises or exits non-zero falls straight through to Path B rather than propagating (spec step 5).
Path B's own `claude -p` call is allowed to block -- that IS path B's contract (Codex "runs one
command and reads the printed result") -- so no timeout is imposed on that leg.

`delegate-claude.ps1`/`.cmd` are shelled to, not
imported or forked -- there is nothing to import across languages (PowerShell vs. Python), so the
tier ladder below is a Python MIRROR of that script's ladder, not a shared source of truth.
Consolidation note for merge: if delegate-claude.ps1's ladder ever changes, `TIER_LADDER` here
needs the matching edit by hand; nothing enforces the two staying in sync today.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import reach as reach_mod

Runner = Callable[..., subprocess.CompletedProcess]

# Mirrors delegate-claude.ps1's ladder: executor -> sonnet (default),
# planner -> claude-opus-5-5, taste -> fable.
TIER_LADDER = {
    "executor": "sonnet",
    "planner": "claude-opus-5-5",
    "taste": "fable",
}
DEFAULT_TIER = "executor"
DEFAULT_PERM = "auto"   # matches delegate-claude's default: the user's normal agent mode, no stalls
REMOTE_TMUX = "/usr/bin/tmux"
FALLBACK_COMMAND = "delegate-claude"
# Reaching the far side is capped hard (reach.py); the launch call itself (one ssh round trip
# that opens a window and returns -- it does not wait for the worker to do anything) gets a
# short cap of its own so a wedged ssh session after a successful probe still cannot hang Codex.
LAUNCH_TIMEOUT_SECONDS = 10


@dataclass
class DelegateResult:
    ok: bool
    mode: str                       # "tmux" (path A) | "fallback" (path B)
    message: str                    # a sentence for Codex to read back, never a traceback
    job_id: Optional[str] = None
    log_path: Optional[str] = None


def resolve_model(tier: Optional[str], model: Optional[str]) -> str:
    """`model` (a raw alias, `-Model` in delegate-claude's own flags) always wins; otherwise the
    tier ladder; an unrecognised tier falls back to the default tier's model rather than raising
    -- a typo in `--tier` should degrade, not break the delegation."""
    if model:
        return model
    return TIER_LADDER.get(tier or DEFAULT_TIER, TIER_LADDER[DEFAULT_TIER])


def env_prefix() -> str:
    """The env-scrub opt-out for the launched child, exactly as delegate-claude.ps1 sets it for
    ITS child: un-scrub, and
    drop the two vars that mark a shell as a Claude subprocess, so the worker starts at its real
    permission mode instead of being forced down to `default`. A fresh tmux window does not
    inherit Codex's already-scrubbed environment, but it DOES inherit the USER-scope
    `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` -- this line is what cancels that, for this one child
    only; it never touches the USER-scope value itself."""
    return "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT;"


def build_claude_invocation(task: str, *, tier: Optional[str] = None, model: Optional[str] = None,
                            perm: Optional[str] = None) -> str:
    """The command line typed into the new tmux window's shell -- env-scrub opt-out, then
    `claude` with the resolved model/permission and the task as its one argument."""
    resolved_model = resolve_model(tier, model)
    resolved_perm = perm or DEFAULT_PERM
    escaped_task = task.replace("\\", "\\\\").replace('"', '\\"')
    return f'{env_prefix()} claude --model {resolved_model} --permission-mode {resolved_perm} "{escaped_task}"'


def job_id_for(project: str, now: Optional[float] = None) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    return f"{stamp}-{project}"


def build_ssh_new_window_command(project: str, directory: str, claude_invocation: str, *,
                                 ssh_bin: Optional[str] = None,
                                 remote_tmux: str = REMOTE_TMUX) -> list[str]:
    """The argv that opens the worker window on the session-0 pantheon server, reached over ssh
    (spec step 2): `ssh localhost 'tmux new-window -n <project> -c <dir> "<claude invocation>"'`.
    The window is named for the project, so it shows in the supervisor like
    any other agent and can be joined into THE PIT."""
    inner = (f'{remote_tmux} new-window -n {shlex.quote(project)} '
             f'-c {shlex.quote(directory)} "{claude_invocation}"')
    return [reach_mod.ssh_path(ssh_bin), "-o", "BatchMode=yes", "localhost", inner]


def build_fallback_command(task: str, *, tier: Optional[str] = None, model: Optional[str] = None,
                           perm: Optional[str] = None, directory: Optional[str] = None,
                           dry_run: bool = False) -> list[str]:
    """The argv for the existing path-B helper -- same flags it already accepts
    : `-Tier`, `-Model`, `-Perm`, `-Dir`, then the task positionally."""
    argv = [FALLBACK_COMMAND]
    if tier:
        argv += ["-Tier", tier]
    if model:
        argv += ["-Model", model]
    if perm:
        argv += ["-Perm", perm]
    if directory:
        argv += ["-Dir", directory]
    if dry_run:
        argv += ["-DryRun"]
    argv.append(task)
    return argv


def _run_fallback(task: str, *, tier: Optional[str], model: Optional[str], perm: Optional[str],
                  directory: Optional[str], runner: Runner, reason: Optional[str]) -> DelegateResult:
    argv = build_fallback_command(task, tier=tier, model=model, perm=perm, directory=directory)
    try:
        completed = runner(argv, timeout=None)
    except (OSError, subprocess.SubprocessError) as exc:
        return DelegateResult(False, "fallback", f"path B ({FALLBACK_COMMAND}) failed to run: {exc}")
    message = (completed.stdout or "").strip() or (completed.stderr or "").strip() \
        or f"{FALLBACK_COMMAND} ran with no output"
    if reason:
        message = f"{reason}\n{message}"
    return DelegateResult(completed.returncode == 0, "fallback", message)


def delegate(task: str, *, tier: Optional[str] = None, model: Optional[str] = None,
            perm: Optional[str] = None, directory: Optional[str] = None,
            project: Optional[str] = None, cfg=None,
            reachable: Optional[Callable[[], bool]] = None,
            runner: Optional[Runner] = None) -> DelegateResult:
    """Detect-tmux-or-background (spec steps 3/5), from ONE call. `reachable`/`runner` are
    injectable for tests (and for a caller that already has its own tmux/subprocess seams); real
    callers leave both at their defaults."""
    task = (task or "").strip()
    if not task:
        return DelegateResult(False, "fallback", "no task given; nothing to delegate")
    reachable = reachable or reach_mod.pantheon_tmux_reachable
    runner = runner or (lambda argv, **kw: subprocess.run(argv, capture_output=True, text=True, **kw))
    directory = directory or os.getcwd()
    project = project or (Path(directory).name or "delegate")

    if reachable():
        invocation = build_claude_invocation(task, tier=tier, model=model, perm=perm)
        argv = build_ssh_new_window_command(project, directory, invocation)
        try:
            completed = runner(argv, timeout=LAUNCH_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return _run_fallback(task, tier=tier, model=model, perm=perm, directory=directory,
                                 runner=runner,
                                 reason=f"tmux was reachable but the launch failed ({exc}); fell back to path B")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            return _run_fallback(task, tier=tier, model=model, perm=perm, directory=directory,
                                 runner=runner,
                                 reason=f"tmux window did not open ({detail or 'non-zero exit'}); fell back to path B")
        job = job_id_for(project)
        log_path = str(Path(cfg.dispatch_dir) / f"{job}.log") if cfg is not None else None
        where = log_path or "state/dispatch/<job>.log"
        return DelegateResult(
            True, "tmux",
            f"worker opened in the pantheon window ({project}); watch it on the deck, or read {where}",
            job_id=job, log_path=log_path,
        )
    return _run_fallback(task, tier=tier, model=model, perm=perm, directory=directory,
                         runner=runner, reason=None)


# ---------------------------------------------------------------------- CLI (`pantheon delegate`)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    from .. import config as config_mod

    parser = argparse.ArgumentParser(prog="pantheon delegate")
    parser.add_argument("task", help="the task text handed to the Claude worker")
    parser.add_argument("--tier", choices=sorted(TIER_LADDER), default=None,
                        help="executor (default) | planner | taste")
    parser.add_argument("--model", default=None, help="a raw model alias; overrides --tier")
    parser.add_argument("--perm", default=None, help="permission mode; default: auto")
    parser.add_argument("--dir", dest="directory", default=None,
                        help="the worker's folder; default: the current directory")
    parser.add_argument("--project", default=None,
                        help="the tmux window name; default: the folder's own name")
    args = parser.parse_args(argv)
    try:
        cfg = config_mod.load()
    except Exception:
        cfg = None
    result = delegate(args.task, tier=args.tier, model=args.model, perm=args.perm,
                      directory=args.directory, project=args.project, cfg=cfg)
    print(result.message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
