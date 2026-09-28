"""`bin/appearance_apply` -- scripted appearance changes, no screen needed
. Each flag maps straight to one of the pure functions
`app.py` also uses for the screen's buttons, so the two never drift apart.
"""
from __future__ import annotations

import argparse
import json
import sys

from .. import config as config_mod
from . import palette
from .app import DEFAULT_PROFILE_NAME, apply_font, apply_preset_action, override_action, reset_all_action, reset_token_action


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pantheon.appearance.apply",
        description="Apply a font and/or palette change, scripted or tested, no screen needed.",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_NAME, help="Windows Terminal profile name")
    parser.add_argument("--preset", help="switch the palette preset (pantheon | high-contrast | light)")
    parser.add_argument("--override", action="append", default=[], metavar="TOKEN=VALUE",
                         help="set one theme token override; repeatable")
    parser.add_argument("--reset-token", action="append", default=[], metavar="TOKEN")
    parser.add_argument("--reset-all", action="store_true")
    parser.add_argument("--font-face")
    parser.add_argument("--font-size", type=float)
    parser.add_argument("--cell-height", type=float)
    parser.add_argument("--cell-width", type=float)
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    results: dict = {}

    if args.preset:
        try:
            results["preset"] = apply_preset_action(args.preset, cfg)
        except palette.UnknownPresetError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    for pair in args.override:
        if "=" not in pair:
            print(f"error: --override wants TOKEN=VALUE, got {pair!r}", file=sys.stderr)
            return 1
        token, value = pair.split("=", 1)
        try:
            results.setdefault("overrides", []).append(override_action(token, value, cfg))
        except palette.UnknownTokenError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    for token in args.reset_token:
        try:
            results.setdefault("reset", []).append(reset_token_action(token, cfg))
        except palette.UnknownTokenError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.reset_all:
        results["reset_all"] = reset_all_action(cfg)

    if any([args.font_face, args.font_size, args.cell_height, args.cell_width]):
        results["font"] = apply_font(
            cfg,
            profile_name=args.profile,
            face=args.font_face,
            size=args.font_size,
            cell_height=args.cell_height,
            cell_width=args.cell_width,
        )
        if not results["font"]["ok"]:
            print(results["font"]["message"], file=sys.stderr)

    if not results:
        parser.print_help()
        return 1

    print(json.dumps(results, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
