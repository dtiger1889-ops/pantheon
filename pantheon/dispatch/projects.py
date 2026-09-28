"""The project picker's data and the "New session" launch.

A project is a folder the user works in: every folder directly under `projects_root` that holds a
`CLAUDE.md` or a git repo, plus any folder an agent has ever been started in (the event log's
`cwd`s), so a project outside the root still shows up once it has been used, plus every folder in
`pinned_projects` (pantheon.toml), which stays listed after the rotated log forgets it. "Most recent" is the
newest `SessionStart` / `dispatch` / `new_session` event for that folder; a folder never used sorts
by its modification time, after every used one.

`start_session` is `dispatch.launch.dispatch` without a queue row: no briefing template, no
tracker id, an optional first message typed into the new session, and a `new_session` line in the
event log so THE PIT can show where it went.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .. import events as events_mod
from ..models import Event, LaunchResult, parse_ts, utcnow_iso
from ..providers import get_providers
from . import briefing as briefing_mod

USED_EVENTS = {"SessionStart", "dispatch", "new_session"}
MARKERS = ("CLAUDE.md", ".git", "AGENTS.md")
WORKSPACE_LABEL = "Claude (workspace)"


@dataclass(frozen=True)
class Project:
    name: str
    path: str                        # forward slashes, as the deck uses everywhere
    last_used: Optional[datetime]    # None = never seen in the event log
    modified: float = 0.0            # folder mtime, the tie-break for never-used folders

    @property
    def last_used_text(self) -> str:
        if self.last_used is None:
            return "never"
        seconds = (datetime.now(timezone.utc) - self.last_used).total_seconds()
        if seconds < 3600:
            return f"{max(1, int(seconds // 60))}m ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86400)}d ago"


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/")


def _is_project(folder: Path) -> bool:
    return any((folder / m).exists() for m in MARKERS)


def last_used_by_folder(events: Iterable[Event]) -> dict[str, datetime]:
    """Newest use per folder from the event log (`cwd` on SessionStart / dispatch / new_session).
    Keys are lower-cased normalised paths; `spelling_by_folder` keeps the real spelling."""
    out: dict[str, datetime] = {}
    for e in events:
        if e.event not in USED_EVENTS or not e.cwd:
            continue
        when = e.when
        if when is None:
            continue
        key = _norm(e.cwd).lower()
        if key not in out or when > out[key]:
            out[key] = when
    return out


def spelling_by_folder(events: Iterable[Event]) -> dict[str, str]:
    """The folder path as the event log spells it (`C:/Users/...`), by lower-cased key -- the
    picker showed `/:/users/...` when a lower-cased key was resolved through MSYS Python
   ."""
    out: dict[str, str] = {}
    for e in events:
        if e.cwd:
            out.setdefault(_norm(e.cwd).lower(), _norm(e.cwd))
    return out


def list_projects(cfg, events: Iterable[Event] = ()) -> list[Project]:
    """Every project folder under `projects_root`, every `pinned_projects` folder, plus every
    folder the log has seen used."""
    used = last_used_by_folder(events)
    seen: dict[str, Project] = {}
    root = Path(str(getattr(cfg, "projects_root", "") or ""))
    if root.is_dir():
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            children = []
        for folder in children:
            if folder.name.startswith((".", "_")) or not _is_project(folder):
                continue
            path = _norm(str(folder))
            try:
                mtime = folder.stat().st_mtime
            except OSError:
                mtime = 0.0
            seen[path.lower()] = Project(folder.name, path, used.get(path.lower()), mtime)
    for pinned in getattr(cfg, "pinned_projects", None) or []:
        folder = Path(str(pinned))
        path = _norm(str(pinned))
        # Pinned by hand, so no marker check; a folder that has since gone is skipped quietly.
        if path.lower() in seen or not folder.is_dir():
            continue
        try:
            mtime = folder.stat().st_mtime
        except OSError:
            mtime = 0.0
        seen[path.lower()] = Project(folder.name, path, used.get(path.lower()), mtime)
    spelling = spelling_by_folder(events)
    root_key = _norm(str(root)).lower() if str(root) else ""
    for key, when in used.items():
        if key in seen:
            continue
        folder = Path(spelling.get(key, key))
        # A folder outside the root counts only if it is a project itself (a CLAUDE.md, a git
        # repo, an AGENTS.md): the log also records shells opened in the home folder, the
        # workspace root, Temp and system32, none of which are projects.
        if not folder.is_dir() or key == root_key or not _is_project(folder):
            continue
        seen[key] = Project(folder.name, _norm(str(folder)), when, 0.0)
    return list(seen.values())


def workspace_project(cfg, events: Iterable[Event] = ()) -> Project:
    """The `projects_root` itself, so the user can start a plain session at the workspace root
    without Ctrl+O -- a fixed entry the picker always shows first, carrying the same recency
    metadata (`last_used`) the scanned folders get."""
    root = Path(str(getattr(cfg, "projects_root", "") or ""))
    path = _norm(str(root))
    used = last_used_by_folder(events)
    try:
        mtime = root.stat().st_mtime
    except OSError:
        mtime = 0.0
    return Project(WORKSPACE_LABEL, path, used.get(path.lower()), mtime)


def sort_projects(projects: Iterable[Project], by: str = "recent") -> list[Project]:
    """`recent`: used folders newest first, then never-used by folder mtime; `name`: A-Z."""
    items = list(projects)
    if by == "name":
        return sorted(items, key=lambda p: p.name.lower())
    far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return sorted(
        items,
        key=lambda p: (p.last_used is None, -(p.last_used or far_past).timestamp(), -p.modified, p.name.lower()),
    )


def filter_projects(projects: Iterable[Project], text: str) -> list[Project]:
    """Every project whose name contains every word typed, in any order, any case."""
    words = [w for w in (text or "").lower().split() if w]
    if not words:
        return list(projects)
    return [p for p in projects if all(w in p.name.lower() or w in p.path.lower() for w in words)]


def write_first_message(cfg, project_dir: str, text: str, now: Optional[datetime] = None) -> Path:
    """The optional first message, saved like a briefing so the same launch path types it."""
    now = now or datetime.now(timezone.utc)
    folder = Path(cfg.dispatch_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = briefing_mod.slug(Path(project_dir).name, 30)
    path = folder / f"{now.strftime('%Y%m%dT%H%M%SZ')}-new-{stem}.md"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def start_session(project_dir: str, cfg, provider, interactive: Optional[bool] = None,
                  first_message: Optional[str] = None,
                  launch_options: Optional[dict] = None) -> LaunchResult:
    """Open a fresh session in `project_dir` with `provider`; no queue row, no tracker id.
    A headless Codex job cannot run without something to do, so it needs `first_message`.
    `launch_options` is recorded on the event only -- applying it to the live window is the
    caller's job (`supervisor/pane.py`'s launch worker), since that needs `session_ctl` and a
    window index this function has no reason to know about."""
    caps = provider.capabilities() if hasattr(provider, "capabilities") else set()
    if interactive is None:
        interactive = "interactive_tmux" in caps
    project_dir = _norm(project_dir)
    if not Path(project_dir).is_dir():
        return LaunchResult(False, message=f"that folder is not there any more: {project_dir}")
    message = (first_message or "").strip()
    if not interactive and not message:
        return LaunchResult(False, message="a headless Codex job needs a first message to work on")
    path = write_first_message(cfg, project_dir, message) if message else None
    chosen_options = {k: v for k, v in (launch_options or {}).items() if v}
    if getattr(provider, "name", "") == "claude":
        # Claude gets model/effort/mode as start flags. Typing `/model` + `/effort` after launch
        # lost the effort whenever a first message was already running.
        for key in ("model", "effort", "mode"):
            if key in chosen_options:
                chosen_options[f"start_{key}"] = chosen_options.pop(key)
    try:
        result = provider.launch(project_dir, str(path) if path else "", interactive,
                                 options=chosen_options)
    except TypeError:
        # A provider that predates launch options (the Protocol's three-argument form).
        result = provider.launch(project_dir, str(path) if path else "", interactive)
    try:
        extra = {
            "provider": getattr(provider, "name", "?"),
            "where": result.where,
            "window_index": result.window_index,
            "first_message": str(path) if path else "",
        }
        chosen = {k: v for k, v in (launch_options or {}).items() if v}
        if chosen:
            extra["launch_options"] = chosen
        events_mod.append_event(
            cfg.events_file,
            Event(
                ts=utcnow_iso(),
                event="new_session" if result.ok else "new_session_failed",
                source="pantheon",
                job_id=result.job_id,
                cwd=project_dir,
                project=Path(project_dir).name,
                tmux_pane=result.tmux_pane,
                message=result.message,
                extra=extra,
            ),
        )
    except OSError:
        pass
    return result


# ---------------------------------------------------------------------- CLI (`pantheon open ...`)
# v2 step 3: a shell-driven way in to the same picker flow, for when the user is not looking at
# the deck at all -- a scheduled task, a script, or a plain shell. `bin/pantheon open ...` execs
# `python -m pantheon.dispatch.projects open ...`; see `bin/pantheon`'s usage comment.

_CLI_WHO: dict[str, tuple[str, Optional[bool]]] = {
    "claude": ("claude", None),
    "codex-pc": ("codex", True),
    "codex-headless": ("codex", False),
}


def _resolve_project(cfg, name_or_path: str) -> str:
    """`<project>` is a folder name under `projects_root`, or a path of its own -- whichever
    resolves to a real folder wins; an unresolved name is passed through as-is so
    `start_session`'s own "that folder is not there any more" refusal names it."""
    candidate = Path(name_or_path)
    if candidate.is_dir():
        return _norm(str(candidate))
    under_root = Path(str(getattr(cfg, "projects_root", "") or "")) / name_or_path
    if under_root.is_dir():
        return _norm(str(under_root))
    return _norm(name_or_path)


def _cli_open(cfg, args) -> int:
    # `--preset NAME` fills in whatever was not typed; a typed flag always wins.
    chosen = {
        "project": args.project, "who": args.who, "model": getattr(args, "model", None),
        "effort": getattr(args, "effort", None), "mode": getattr(args, "mode", None),
        "message": args.message or None,
    }
    if getattr(args, "preset", None):
        from . import presets as presets_mod
        try:
            chosen = presets_mod.resolve(args.preset, cfg, chosen)
        except presets_mod.PresetError as exc:
            print(exc)
            return 2
    if not chosen.get("project"):
        print("which project? name a folder, or use a preset that names one")
        return 2
    who = chosen.get("who") or "claude"
    if who not in _CLI_WHO:
        print(f"'{who}' is not someone this deck can start; pick one of: {', '.join(sorted(_CLI_WHO))}")
        return 2
    project_dir = _resolve_project(cfg, chosen["project"])
    provider_name, interactive = _CLI_WHO[who]
    # `get_providers` (this module's own name, imported at the top) rather than a value already
    # resolved by the caller -- so `monkeypatch.setattr(projects_mod, "get_providers", fake)`
    # reaches this call the way a test would expect.
    providers = get_providers(cfg)
    provider = providers.get(provider_name)
    if provider is None:
        print(f"'{provider_name}' is not switched on in pantheon.toml")
        return 1
    options = {k: chosen.get(k) for k in ("model", "effort", "mode")
               if chosen.get(k) and chosen.get(k) != "default"}
    resume = getattr(args, "resume", None)
    if resume:
        options["resume"] = resume
    result = start_session(project_dir, cfg, provider, interactive, chosen.get("message") or None,
                           launch_options=options or None)
    # Model/effort/mode went on the command line inside `start_session`; nothing to type after.
    print(result.message)
    return 0 if result.ok else 1


def build_parser(prog: str = "pantheon.dispatch.projects"):
    import argparse

    parser = argparse.ArgumentParser(prog=prog)
    sub = parser.add_subparsers(dest="command", required=True)
    open_p = sub.add_parser("open", help="start a fresh session in a project folder")
    open_p.add_argument("project", nargs="?", default=None,
                        help="a folder name under projects_root, or a path of its own "
                             "(may be left out when --preset names one)")
    open_p.add_argument("--who", choices=sorted(_CLI_WHO), default=None,
                        help="who works it (default: the preset's, else claude)")
    open_p.add_argument("--resume", nargs="?", const=True, default=None, metavar="SESSION_ID",
                        help="reopen a Claude session in that folder: the newest, or the given id")
    open_p.add_argument("--message", default="",
                        help="the first message typed into it (required for codex-headless)")
    open_p.add_argument("--model", default=None, help="model for the new session, e.g. sonnet")
    open_p.add_argument("--effort", default=None, help="effort for the new session, e.g. high")
    open_p.add_argument("--mode", default=None,
                        help="permission mode (Claude) or sandbox (Codex) for the new session")
    open_p.add_argument("--preset", default=None,
                        help="a [presets.<name>] table in pantheon.toml; typed flags win over it")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    from .. import config as config_mod

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "open":
        return _cli_open(config_mod.load(), args)
    parser.error(f"unknown command {args.command!r}")   # pragma: no cover - argparse already refuses this
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
