"""Prints how many tasks are on each tab, so the numbers can be checked against Obsidian.

This is the checking tool for the queue pane: run it, put it beside the Sprints Base open on
screen, and the counts should match tab for tab. `bin/queue_counts` is the shell wrapper.

It deliberately uses the same task source and the same tabs the queue pane uses -- if it printed
its own version of the rules it would prove nothing.
"""
from __future__ import annotations

import argparse
import sys

from .. import config
from ..models import TaskRow
from ..tasks import checkpoint_source, get_task_source
from . import filters as spec_filters


def _first_names(rows: list[TaskRow], how_many: int = 3) -> str:
    names = [r.id for r in rows[:how_many]]
    return ", ".join(names) if names else "(none)"


def _print_checkpoint(cfg, project: str) -> int:
    """the four drill tabs' counts for `project`'s own `CHECKPOINT.md` `## Open
    threads` -- the same source and tabs the drilled queue would show, so a parse can be checked
    against the file by hand."""
    source = checkpoint_source.make(cfg, project)
    rows = source.rows()
    print(f"source: {source.kind}")
    print(f"file: {source.path}")
    if source.notice:
        print(source.notice)
    print(f"threads read: {len(rows)}   could not parse: {source.parse_errors()}")
    print()
    print(f"{'key':<4}{'tab':<18}{'count':>6}   first 3 threads")
    print("-" * 78)
    for tab in source.tabs():
        keep = sorted([r for r in rows if tab.filter(r)], key=tab.sort_key)
        titles = [((r.summary or "")[:40]) for r in keep[:3]] or ["(none)"]
        print(f"{tab.key:<4}{tab.name:<18}{len(keep):>6}   {', '.join(titles)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Count the tasks on each queue tab and show the first few, "
                    "so the numbers can be compared with Obsidian."
    )
    parser.add_argument("--config", help="path to a pantheon.toml (defaults to the project's own)")
    parser.add_argument("--vault", help="read a different vault folder's parent, for testing")
    parser.add_argument("--base", help="read a specific .base file (any absolute path) instead of "
                        "pantheon.toml's own base_file, so a second Base can be checked before "
                        "switching to it (S11 step 2)")
    parser.add_argument("--spec", action="store_true",
                        help="use the rules written in the specs instead of the ones in the Base file")
    parser.add_argument("--checkpoint", metavar="PROJECT",
                        help="S21: count a project's own CHECKPOINT.md `## Open threads` instead "
                             "of the vault-wide Sprints view -- the drilled-queue's own numbers, "
                             "to check a parse against the file by hand")
    args = parser.parse_args(argv)

    cfg = config.load(args.config) if args.config else config.load()
    if args.checkpoint:
        return _print_checkpoint(cfg, args.checkpoint)
    if args.vault:
        cfg = config.Config(**{**cfg.__dict__, "vault": args.vault})
    if args.base:
        # An absolute path here makes `Path(vault) / base_file` resolve to exactly that path
        # (pathlib drops the left side of `/` when the right side is already absolute), so
        # `--base` works for a Base anywhere, not only ones under the configured vault; the
        # folder is re-derived from THIS Base's own `file.inFolder`, not whatever is configured.
        cfg = config.Config(**{**cfg.__dict__, "base_file": args.base, "sprints_folder": None})

    try:
        source = get_task_source(cfg)
    except Exception as exc:
        print(f"could not read the task list: {exc}", file=sys.stderr)
        return 2

    rows = source.rows()
    load_error = getattr(source, "load_error", "")
    if load_error:
        print(load_error, file=sys.stderr)
        return 2

    flagged = getattr(source, "flagged_tabs", lambda: set())()
    print(f"source: {source.kind}")
    print(f"folder: {getattr(source, 'folder', cfg.sprints_dir)}")
    print(f"tasks read: {len(rows)}   files that would not read: {source.parse_errors()}")
    print()
    print(f"{'key':<4}{'tab':<18}{'count':>6}   first 3 file names")
    print("-" * 78)

    if args.spec:
        pairs = [(str(i + 1), name, spec_filters.select(rows, name))
                 for i, name in enumerate(spec_filters.TAB_ORDER)]
    else:
        pairs = []
        for tab in source.tabs():
            keep = sorted([r for r in rows if tab.filter(r)], key=tab.sort_key)
            pairs.append((tab.key or "-", tab.name, keep))

    for key, name, kept in pairs:
        label = name + ("*" if name in flagged else "")
        print(f"{key:<4}{label:<18}{len(kept):>6}   {_first_names(kept)}")
        group_by = None
        for tab in source.tabs():
            if tab.name == name:
                group_by, group_order = tab.group_by, tab.group_order
                break
        else:
            group_by, group_order = spec_filters.GROUPS.get(name), spec_filters.GROUP_ORDER.get(name, [])
        if group_by is not None and group_order:
            for heading in group_order:
                members = [r for r in kept if group_by(r) == heading]
                print(f"{'':<4}  {heading:<16}{len(members):>6}   {_first_names(members)}")

    if flagged:
        print()
        print("* this tab has a rule in the Base file the deck could not reproduce; "
              "the ignored rules are in state/pantheon.log")
    errors = getattr(source, "error_files", lambda: {})()
    if errors:
        print()
        print("files that would not read (usually a note being saved right now):")
        for name, why in errors.items():
            print(f"  {name}: {why}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
