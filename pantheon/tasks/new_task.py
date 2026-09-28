"""`pantheon new "summary" --project x [--tier now] [--agent]`: write one task file
into the standalone folder without opening Obsidian at all. Refuses outright when that folder
would resolve to the vault's own Sprints folder -- a bad
`[standalone] folder` edit in `pantheon.toml` should never turn into a vault write.

The file this writes carries the same frontmatter fields `pantheon/tasks/obsidian_base.py` already
reads (`row_from_post`), so it shows up the next time the standalone source refreshes with no
special-casing.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

from .. import config

TEMPLATE = """\
---
summary: "{summary}"
project: {project}
tier: {tier}
status: open
complexity: quick
agent: {agent}
done: false
next: false
picked:
due:
created: {created}
note:
reply:
---
"""

_SLUG_STRIP = re.compile(r"[^A-Za-z0-9 _-]")


def _slug(summary: str) -> str:
    text = _SLUG_STRIP.sub("", summary).strip()
    return text or "task"


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _inside(folder: Path, root: Path) -> bool:
    """True when `folder` is `root` or anywhere below it (compared after resolving symlinks and
    junctions, so a junction into the vault does not slip past)."""
    try:
        folder.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _unique_path(folder: Path, slug: str) -> Path:
    """`<slug>.md`, or `<slug>-2.md`, `<slug>-3.md`, ... the first name nothing else is using."""
    path = folder / f"{slug}.md"
    n = 2
    while path.exists():
        path = folder / f"{slug}-{n}.md"
        n += 1
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pantheon new",
        description="Write one task file into the standalone folder -- no Obsidian required.",
    )
    parser.add_argument("summary", help="one line describing the task")
    parser.add_argument("--project", required=True, help="which project folder this belongs to")
    parser.add_argument("--tier", default="now", choices=["now", "soon", "someday"])
    parser.add_argument("--agent", "--fable", dest="agent", action="store_true",
                        help="hand it to the Agent's plate (--fable is the old name)")
    parser.add_argument("--config", help="path to a pantheon.toml (defaults to the project's own)")
    args = parser.parse_args(argv)

    cfg = config.load(args.config) if args.config else config.load()
    folder = Path(cfg.standalone_settings().folder)
    vault_sprints = Path(cfg.vault) / (cfg.sprints_folder or "Projects/Sprints")
    if _same_folder(folder, vault_sprints) or _inside(folder, Path(cfg.vault)):
        # Not just the Sprints folder: ANY folder inside the vault. D7 says the vault is never
        # written, and a standalone folder parked at `<vault>/Projects` would be exactly that.
        print(
            f"refused: {folder} is inside the vault; the vault is never written to "
            f"(D7). Set [standalone] folder in pantheon.toml to a path outside it first.",
            file=sys.stderr,
        )
        return 2

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"could not create {folder}: {exc}", file=sys.stderr)
        return 2

    path = _unique_path(folder, _slug(args.summary))
    text = TEMPLATE.format(
        summary=args.summary.replace('"', '\\"'),
        project=args.project,
        tier=args.tier,
        agent="true" if args.agent else "false",
        created=date.today().isoformat(),
    )
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
