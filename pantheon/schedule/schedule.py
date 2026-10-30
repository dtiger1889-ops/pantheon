"""Timed starts through Windows Task Scheduler.

One start = one one-shot Task Scheduler entry (`schtasks /create /sc once`) plus one note in
`state/scheduled/<id>.json`. No always-on process of our own, no new tmux window, nothing in
`bin/pantheon` or `tmuxctl.py`.

Why the task runs `ssh localhost ... bin/schedule --fire <id>` and not `pantheon open` directly:
Task Scheduler starts the task in the user's desktop Windows session, but the pantheon tmux server
lives on the SSH side (Windows session 0), and MSYS2 programs in different Windows sessions cannot
share a tmux socket. Going through `ssh localhost` lands the start
on the same side as the deck, the same route `pantheon --restart` takes from a desktop session.
Firing by id also keeps the task's own command short (Task Scheduler caps it near 260
characters) and keeps a typed first message out of three layers of quoting: the note holds the
exact `pantheon open ...` arguments.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..dispatch import briefing as briefing_mod

TASK_PREFIX = "pantheon-start-"
# Called by this Python (MSYS2's, `os.name == "posix"`, so an MSYS path); `SSH` is run later by
# Task Scheduler itself, so it keeps its Windows spelling.
SCHTASKS = "/c/Windows/System32/schtasks.exe" if os.name == "posix" else "C:\\Windows\\System32\\schtasks.exe"
SSH = "C:\\Windows\\System32\\OpenSSH\\ssh.exe"
REPO_ROOT = Path(__file__).resolve().parents[2]

Runner = Callable[[list[str]], subprocess.CompletedProcess]


@dataclass
class ScheduleResult:
    ok: bool
    message: str
    id: Optional[str] = None
    manifest: dict = field(default_factory=dict)


# ------------------------------------------------------------------------------ building blocks

def scheduled_dir(cfg) -> Path:
    return Path(cfg.state_dir) / "scheduled"


def open_args(project_dir: str, who: str = "claude", options: Optional[dict] = None,
              message: str = "") -> list[str]:
    """The arguments after `pantheon`, exactly as the fire will pass them to `pantheon open`."""
    args = ["open", str(project_dir).replace("\\", "/"), "--who", who or "claude"]
    for key in ("model", "effort", "mode"):
        value = (options or {}).get(key)
        if value and value != "default":
            args += [f"--{key}", str(value)]
    if (message or "").strip():
        args += ["--message", message.strip()]
    return args


def command_line(args: list[str]) -> str:
    """What a person would type: `pantheon open C:/Users/.../x --model sonnet --effort high`."""
    return shlex.join(["pantheon", *args])


def msys_path(path: str | Path) -> str:
    """`C:/Home/x` or `C:\\Home\\x` -> `/c/Home/x` for the MSYS2 shell on the far side of ssh;
    a path that is already MSYS-shaped (`/tmp/pw/...`, `/c/...`) is left alone."""
    text = str(path).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


def fire_command(task_id: str, root: str | Path = REPO_ROOT) -> str:
    """The one line Task Scheduler runs at the chosen minute."""
    remote = f"export PATH=/usr/bin:$PATH; {msys_path(root)}/bin/schedule --fire {task_id}"
    return f'{SSH} -o BatchMode=yes localhost "{remote}"'


def schtasks_create_argv(task_id: str, at: datetime, task_run: str) -> list[str]:
    return [SCHTASKS, "/create", "/sc", "once", "/st", at.strftime("%H:%M"),
            "/sd", at.strftime("%m/%d/%Y"), "/tn", task_id, "/tr", task_run, "/f"]


def schtasks_delete_argv(task_id: str) -> list[str]:
    return [SCHTASKS, "/delete", "/tn", task_id, "/f"]


def run_schtasks(argv: list[str]) -> subprocess.CompletedProcess:
    """The real call. `MSYS2_ARG_CONV_EXCL=*` stops MSYS2 Python rewriting `/create`, `/tn` and
    the rest into `C:/msys64/create`-style paths on their way to a Windows program. The MSYS2
    runtime reads it from THIS process's own environment at spawn time, so it is set here, and put back afterwards."""
    before = os.environ.get("MSYS2_ARG_CONV_EXCL")
    os.environ["MSYS2_ARG_CONV_EXCL"] = "*"
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))
    finally:
        if before is None:
            os.environ.pop("MSYS2_ARG_CONV_EXCL", None)
        else:
            os.environ["MSYS2_ARG_CONV_EXCL"] = before


def _said(proc: subprocess.CompletedProcess) -> str:
    return " ".join(((proc.stderr or "") + " " + (proc.stdout or "")).split()) or f"exit {proc.returncode}"


def _manifest_path(cfg, task_id: str) -> Path:
    return scheduled_dir(cfg) / f"{task_id}.json"


def _write(cfg, manifest: dict) -> None:
    folder = scheduled_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(cfg, manifest["id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read(cfg, task_id: str) -> Optional[dict]:
    try:
        return json.loads(_manifest_path(cfg, task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------------- the actions

def create(cfg, project_dir: str, at: datetime, who: str = "claude", options: Optional[dict] = None,
           message: str = "", warning: Optional[str] = None, run: Optional[Runner] = None,
           task_name: Optional[str] = None, now: Optional[float] = None) -> ScheduleResult:
    """Register one start at `at` (local time) and write its note. Nothing is written when
    Task Scheduler refuses."""
    run = run or run_schtasks
    stamp = int(now if now is not None else time.time())
    task_id = task_name or f"{TASK_PREFIX}{briefing_mod.slug(Path(str(project_dir)).name, 30)}-{stamp}"
    args = open_args(project_dir, who, options, message)
    fields = {
        "project": Path(str(project_dir)).name,
        "project_dir": str(project_dir).replace("\\", "/"),
        "who": who or "claude",
        "options": {k: v for k, v in (options or {}).items() if v},
        "message": (message or "").strip(),
        "open_args": args,
        "command": command_line(args),
        "warning": warning or "",
    }
    return _register(cfg, task_id, at, fields, run, f"{fields['project']} starts")


def _register(cfg, task_id: str, at: datetime, fields: dict, run: Runner, verb: str) -> ScheduleResult:
    """The one Task Scheduler entry + the note beside it, for every kind of timed job."""
    task_run = fire_command(task_id)
    proc = run(schtasks_create_argv(task_id, at, task_run))
    if proc.returncode != 0:
        return ScheduleResult(False, f"Task Scheduler would not take it: {_said(proc)}", task_id)
    manifest = {"id": task_id, "at": at.isoformat(timespec="minutes"), **fields, "task_run": task_run,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "status": "waiting"}
    _write(cfg, manifest)
    return ScheduleResult(True, f"{verb} {at.strftime('%a %d %b %H:%M')}", task_id, manifest)


def create_prompt(cfg, target: str, prompt: str, at: datetime, project_dir: Optional[str] = None,
                  who: str = "claude", options: Optional[dict] = None, or_new: bool = False,
                  warning: Optional[str] = None, run: Optional[Runner] = None,
                  now: Optional[float] = None, pane_id: Optional[str] = None) -> ScheduleResult:
    """Register a prompt to be typed into the EXISTING session whose window is named `target`
    at `at`. With `or_new` (and a `project_dir`) a gone session is replaced by a new one that
    gets the same text as its first message. The note holds the prompt itself. `pane_id`, when
    tmux answered at scheduling time, pins the prompt to that pane: a different session that
    later takes the name counts as the old one being gone."""
    run = run or run_schtasks
    stamp = int(now if now is not None else time.time())
    task_id = f"{TASK_PREFIX}prompt-{briefing_mod.slug(target, 30)}-{stamp}"
    fields = {
        "kind": "prompt",
        "project": target,
        "target": target,
        "prompt": (prompt or "").strip(),
        "or_new": bool(or_new),
        "who": who or "claude",
        "options": {k: v for k, v in (options or {}).items() if v},
        "warning": warning or "",
    }
    if pane_id:
        fields["pane_id"] = pane_id
    if or_new and project_dir:
        args = open_args(project_dir, who, options, prompt)
        fields.update(project_dir=str(project_dir).replace("\\", "/"), open_args=args)
    shown = fields["prompt"].replace("\n", " ")
    fields["command"] = f"type into '{target}': {shown[:60]}{'...' if len(shown) > 60 else ''}"
    return _register(cfg, task_id, at, fields, run, f"prompt for '{target}' is typed in")


def list_staged(cfg) -> list[dict]:
    """Every note in `state/scheduled/`, soonest first."""
    folder = scheduled_dir(cfg)
    out: list[dict] = []
    if folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda m: (m.get("at") or "", m.get("id") or ""))


def cancel(cfg, task_id: str, run: Optional[Runner] = None) -> ScheduleResult:
    """Delete the Task Scheduler entry and the note. A start that already fired (its entry gone
    with it) still has its note removed; a waiting one whose entry would not delete keeps its
    note, so nothing is left scheduled that the list no longer shows."""
    run = run or run_schtasks
    manifest = read(cfg, task_id)
    proc = run(schtasks_delete_argv(task_id))
    waiting = manifest is None or manifest.get("status") == "waiting"
    if proc.returncode != 0 and waiting:
        return ScheduleResult(False, f"Task Scheduler would not delete {task_id}: {_said(proc)}", task_id)
    try:
        _manifest_path(cfg, task_id).unlink()
    except FileNotFoundError:
        pass
    return ScheduleResult(True, f"cancelled {task_id}", task_id, manifest or {})


def _default_opener(args: list[str]) -> tuple[int, str]:
    from ..dispatch import projects as projects_mod

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = projects_mod.main(args)
    return code, buffer.getvalue().strip()


def fire(cfg, task_id: str, run: Optional[Runner] = None,
         opener: Optional[Callable[[list[str]], tuple[int, str]]] = None,
         deliverer: Optional[Callable[..., tuple[int, str]]] = None) -> ScheduleResult:
    """What the Task Scheduler entry runs: start the session its note describes (or, for a prompt
    note, type the prompt into the existing session), write down how it went, and remove the
    one-shot entry so Task Scheduler does not fill up with spent ones."""
    run = run or run_schtasks
    opener = opener or _default_opener
    manifest = read(cfg, task_id)
    if manifest is None:
        return ScheduleResult(False, f"no note for {task_id} in {scheduled_dir(cfg)}; nothing started", task_id)
    if manifest.get("status") != "waiting":
        return ScheduleResult(False, f"{task_id} already ran ({manifest.get('status')}); nothing started",
                              task_id, manifest)
    # Claim the fire atomically: two `--fire` runs that both read "waiting" cannot both go on
    # The loser re-reads the note the winner writes.
    claim = _manifest_path(cfg, task_id).with_suffix(".firing")
    try:
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return ScheduleResult(False, f"{task_id} is already being fired; nothing started", task_id, manifest)
    try:
        manifest = read(cfg, task_id)      # read again under the claim: an earlier fire may have finished
        if manifest is None or manifest.get("status") != "waiting":
            return ScheduleResult(False, f"{task_id} already ran; nothing started", task_id, manifest or {})
        return _fire_claimed(cfg, task_id, manifest, run, opener, deliverer)
    finally:
        with contextlib.suppress(OSError):
            claim.unlink()


def _fire_claimed(cfg, task_id: str, manifest: dict, run: Runner, opener, deliverer) -> ScheduleResult:
    prompt_job = manifest.get("kind") == "prompt"
    try:
        if prompt_job:
            from . import deliver as deliver_mod

            manifest["status"] = "trying"       # --list shows a prompt that is waiting for an idle prompt
            _write(cfg, manifest)
            if deliverer is None:
                # `cancel` deletes the note; the retry loop stops at its next look once it is gone.
                code, said = deliver_mod.run(cfg, manifest, opener=opener,
                                             cancelled=lambda: not _manifest_path(cfg, task_id).exists())
            else:
                code, said = deliverer(cfg, manifest, opener=opener)
        else:
            code, said = opener(list(manifest.get("open_args") or []))
    except Exception as exc:   # the note must say what happened, whatever happened
        code, said = 1, f"{type(exc).__name__}: {exc}"
    if prompt_job and not _manifest_path(cfg, task_id).exists():
        # Cancelled while it waited for an idle prompt: writing the note back would undo the cancel.
        run(schtasks_delete_argv(task_id))
        return ScheduleResult(False, said or "cancelled", task_id, manifest)
    if prompt_job:
        manifest["status"] = "sent" if code == 0 else "did not send"
    else:
        manifest["status"] = "started" if code == 0 else "did not start"
    manifest["result"] = said
    manifest["fired_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write(cfg, manifest)
    run(schtasks_delete_argv(task_id))
    return ScheduleResult(code == 0, said or manifest["status"], task_id, manifest)
