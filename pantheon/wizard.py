"""the first-run wizard. `bin/pantheon` runs this, before it does anything else,
whenever `pantheon.toml` is missing -- three plain questions, each with the detected default
already typed in, so pressing Enter three times leaves a working file. It only ever WRITES
`pantheon.toml`: the vault is read once, to check the default folder is really there and say so,
and nothing under it is ever opened or written.

    python -m pantheon.wizard            ask the three questions
    python -m pantheon.wizard --yes      accept every default, ask nothing (for scripts)
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, Optional

from . import config as config_mod


def _ask(question: str, default: str, read_line: Callable[[str], str]) -> str:
    """One question with its default already typed in -- Enter alone keeps it."""
    answer = read_line(f"{question} [{default}]: ").strip()
    return answer or default


def _norm_path(text: str) -> str:
    """Windows paths typed with backslashes would break the TOML string they land in (`\\U` is a
    unicode escape, `\\D` is not a valid escape at all) -- normalize to forward slashes, same as
    every path already in `pantheon.toml`."""
    return text.replace("\\", "/")


def render(vault: str, projects_root: str, tmux_session: str) -> str:
    """`pantheon.toml`'s text, in the same shape as the file the user already has: the same keys,
    the same comments, the same block order (top-level, `[providers]`, `[governor]`, `[notify]`,
    `[appearance]`, `[standalone]`, `[tools]`). Only the three answered questions are not from
    `config.py`'s own dataclass defaults, so a new field added there only ever needs a new line
    here, never a rewrite of this template."""
    d = config_mod.Config()
    gov = config_mod.Governor()
    notify = config_mod.Notify()
    appearance = config_mod.Appearance()
    standalone = config_mod.Standalone()
    tools = config_mod.Tools()

    def flag(value: bool) -> str:
        return "true" if value else "false"

    quiet_hours = ", ".join(f'"{h}"' for h in notify.quiet_hours)

    return f'''# Pantheon settings. Absolute paths only; `~` is never used.
vault = "{vault}"
projects_root = "{projects_root}"
claude_home = "{d.claude_home}"
codex_home = "{d.codex_home}"
tmux_session = "{tmux_session}"
refresh_seconds = {d.refresh_seconds}
ccusage_version = "{d.ccusage_version}"
default_provider = "{d.default_provider}"
max_agents = {d.max_agents}              # dispatch refuses a 5th concurrent agent
task_source = "{d.task_source}"  # "obsidian_base" | "standalone"; stays on Obsidian for now
base_file = "{d.base_file}"  # any .base under `vault`; sprints_folder below is derived
                                     # from ITS OWN file.inFolder(...) filter when left unset
# sprints_folder = "Projects/Sprints"  # only needed if a Base has no file.inFolder filter to derive from

[providers]
claude = {flag(d.providers.get("claude", True))}
codex = {flag(d.providers.get("codex", True))}
ollama = {flag(d.providers.get("ollama", False))}

[governor]                  # session-limit governor
enabled = {flag(gov.enabled)}
dry_run = {flag(gov.dry_run)}
grace_seconds = {gov.grace_seconds}         # how long a session gets to finish /checkpoint before it counts as parked
auto_resume = {flag(gov.auto_resume)}          # false = park and stop; the user restarts by hand
max_parked = {gov.max_parked}              # refuse to park more than this; warn instead (never kill)
resume_prompt = "{gov.resume_prompt}"

[governor.wind_down_at_percent]
five_hour = {gov.wind_down_at_percent["five_hour"]:g}
seven_day = {gov.wind_down_at_percent["seven_day"]:g}

[notify]                    # notifications
enabled = {flag(notify.enabled)}              # ships OFF -- the user turns it on after reading a day of dry-run sent.jsonl
dry_run = {flag(notify.dry_run)}                # write state/notify/sent.jsonl, send nothing
needs_you_after_seconds = {notify.needs_you_after_seconds}
nudge_minutes = {notify.nudge_minutes}
max_nudges = {notify.max_nudges}
present_seconds = {notify.present_seconds}
quiet_hours = [{quiet_hours}]

[notify.toast]
enabled = {flag(notify.toast_enabled)}               # desk only; needs a Windows session (skipped silently over pure SSH)

[notify.ntfy]
enabled = {flag(notify.ntfy_enabled)}
url = "{notify.ntfy_url}"                      # the Tailscale address of the self-hosted ntfy; empty = off
topic = "{notify.ntfy_topic}"

[notify.telegram]
enabled = {flag(notify.telegram_enabled)}
chat_id = "{notify.telegram_chat_id}"                  # your notifier's chat id
credential_target = "{notify.telegram_credential_target}"        # Windows Credential Manager target name for the bot token; never the token itself

[appearance]                # also Ctrl+P -> "theme" in the deck for a live preview
theme = "{appearance.theme}"          # "pantheon" | "high-contrast" | "light"
glyphs = "{appearance.glyphs}"          # "unicode" | "ascii" (Android SSH clients vary)

[standalone]                 # the folder `pantheon new` writes into and, if task_source above
                            # is switched to "standalone", where the queue's rows and tabs come
                            # from instead of Obsidian. Not in use while task_source stays "obsidian_base".
folder = "{standalone.folder}"

[tools]
ccusage = "{tools.ccusage}"  # "" = via npx; point this at a local install once one exists
'''


def run(
    target: Path,
    accept_defaults: bool = False,
    read_line: Callable[[str], str] = input,
    defaults: Optional[config_mod.Config] = None,
) -> Path:
    """Ask the three questions (or, with `accept_defaults`, ask nothing), write `pantheon.toml`
    at `target`, and hand back that path. `target` is always explicit -- never guessed -- so a
    test can point this at a temp folder and the real project's file is never at risk; `defaults`
    is the same idea for the suggested answers themselves."""
    defaults = defaults or config_mod.Config()
    print("Pantheon needs three answers before its first start. Press Enter to keep a suggested answer.")
    if Path(defaults.vault).is_dir():
        print(f"found your vault at {defaults.vault}")
    else:
        print(f"no folder at {defaults.vault}. Type the folder that holds your notes, "
              "or press Enter to keep this path and fix it later in pantheon.toml")

    if accept_defaults:
        vault, projects_root, tmux_session = defaults.vault, defaults.projects_root, defaults.tmux_session
        print("--yes: using the suggested answers above for everything.")
    else:
        vault = _norm_path(_ask("Where is your Obsidian vault?", defaults.vault, read_line))
        projects_root = _norm_path(
            _ask("Where do your project folders live?", defaults.projects_root, read_line)
        )
        tmux_session = _ask("What should the tmux session be called?", defaults.tmux_session, read_line)

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(vault, projects_root, tmux_session), encoding="utf-8")
    print(f"\nsaved your settings to {target}; change them there any time")
    print(
        "note: claude_home, codex_home and [tools] (wt, codex, python) were not asked above -- "
        "check they match your machine in that file (see pantheon.example.toml for what each one is)."
    )
    return target


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pantheon.wizard",
        description="First-run setup: three questions, then writes pantheon.toml.",
    )
    parser.add_argument("--yes", action="store_true", help="accept every default and ask nothing")
    parser.add_argument(
        "--target", help="where to write the file (defaults to the project's own pantheon.toml)"
    )
    args = parser.parse_args(argv)
    target = Path(args.target) if args.target else config_mod.DEFAULT_TOML
    # `input` is looked up here, at call time, not baked in as `run`'s own default -- so a test
    # that monkeypatches `builtins.input` to fake stdin reaches it (a default-argument value is
    # captured once, at import time, and would miss a patch applied afterwards).
    run(target, accept_defaults=args.yes, read_line=input)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
