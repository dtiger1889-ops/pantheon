"""`bin/schedule` -- start a session at a set time. Runs once and
exits; the waiting is Windows Task Scheduler's.

    bin/schedule <project> --at 06:00 [--who claude|codex-pc|codex-headless]
                 [--model M] [--effort E] [--mode MODE] [--message TEXT] [--preset NAME] [--force]
    bin/schedule --list              what is waiting, and how the ones that ran went
    bin/schedule --cancel <id>       take one off (the id is in --list)
    bin/schedule --fire <id>         what the Task Scheduler entry runs; not for typing by hand

When the chosen time lands inside a usage window already past the governor's wind-down line,
it prints why and schedules nothing unless `--force` is given.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..dispatch import presets as presets_mod
from ..dispatch import projects as projects_mod
from . import plan
from . import schedule as schedule_mod

_PROVIDER_FOR_WHO = {"claude": "claude", "codex-pc": "codex", "codex-headless": "codex"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schedule",
        description="Start a Claude or Codex session at a set time, through Windows Task Scheduler.",
    )
    parser.add_argument("project", nargs="?", default=None,
                        help="a folder name under projects_root, or a path (may be left out when --preset names one)")
    parser.add_argument("--at", help="the time to start, HH:MM, 24-hour, local; tomorrow if already past today")
    parser.add_argument("--who", choices=sorted(_PROVIDER_FOR_WHO), default=None,
                        help="who works it (default: the preset's, else claude)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--mode", default=None)
    parser.add_argument("--message", default=None, help="the first message typed into it")
    parser.add_argument("--preset", default=None, help="a [presets.<name>] table in pantheon.toml")
    parser.add_argument("--force", action="store_true",
                        help="schedule it even when the time lands in a nearly spent usage window")
    parser.add_argument("--list", action="store_true", help="show what is waiting and what already ran")
    parser.add_argument("--cancel", metavar="ID", default=None, help="take one scheduled start off")
    parser.add_argument("--fire", metavar="ID", default=None, help=argparse.SUPPRESS)
    return parser


def _when(text: str) -> str:
    try:
        return datetime.fromisoformat(text).strftime("%a %d %b %H:%M")
    except (TypeError, ValueError):
        return text or "?"


def _show_list(cfg) -> int:
    items = schedule_mod.list_staged(cfg)
    if not items:
        print("nothing scheduled")
        return 0
    for m in items:
        choices = "/".join(v for v in (m.get("options") or {}).values() if v)
        line = f"{_when(m.get('at'))}  {m.get('project')}  {m.get('who')}"
        if choices:
            line += f" {choices}"
        line += f"  {m.get('status')}  {m.get('id')}"
        print(line)
        if m.get("result"):
            print(f"    {m['result']}")
    return 0


def _create(cfg, args) -> int:
    if not args.at:
        print("when? add --at HH:MM, e.g. --at 06:00")
        return 2
    chosen = {"project": args.project, "who": args.who, "model": args.model,
              "effort": args.effort, "mode": args.mode, "message": args.message}
    if args.preset:
        try:
            chosen = presets_mod.resolve(args.preset, cfg, chosen)
        except presets_mod.PresetError as exc:
            print(exc)
            return 2
    if not chosen.get("project"):
        print("which project? name a folder, or use a preset that names one")
        return 2
    who = chosen.get("who") or "claude"
    if who not in _PROVIDER_FOR_WHO:
        print(f"'{who}' is not someone this deck can start; pick one of: {', '.join(sorted(_PROVIDER_FOR_WHO))}")
        return 2
    project_dir = projects_mod._resolve_project(cfg, chosen["project"])
    if not Path(project_dir).is_dir():
        print(f"that folder is not there: {project_dir}")
        return 2
    message = chosen.get("message") or ""
    if who == "codex-headless" and not message.strip():
        print("a headless Codex job needs a first message to work on; add --message")
        return 2
    try:
        at = plan.parse_at(args.at)
    except ValueError as exc:
        print(exc)
        return 2
    warning = plan.warn_for(at, cfg, provider=_PROVIDER_FOR_WHO[who])
    if warning:
        print(warning)
        if not args.force:
            print("Nothing was scheduled. Add --force to schedule it anyway, or pick a later time.")
            return 1
    options = {k: chosen.get(k) for k in ("model", "effort", "mode") if chosen.get(k)}
    result = schedule_mod.create(cfg, project_dir, at, who, options, message, warning=warning)
    if not result.ok:
        print(result.message)
        return 1
    print(f"scheduled: {result.message}")
    print(f"  runs: {result.manifest.get('command')}")
    print(f"  take it off with: bin/schedule --cancel {result.id}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    from .. import config as config_mod

    args = build_parser().parse_args(argv)
    cfg = config_mod.load()
    if args.fire:
        result = schedule_mod.fire(cfg, args.fire)
        print(result.message)
        return 0 if result.ok else 1
    if args.list:
        return _show_list(cfg)
    if args.cancel:
        result = schedule_mod.cancel(cfg, args.cancel)
        print(result.message)
        return 0 if result.ok else 1
    return _create(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
