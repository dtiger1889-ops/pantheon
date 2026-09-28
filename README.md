# Pantheon

A terminal-first command deck for driving several coding agents at once. Pantheon runs inside tmux and puts, on one screen:

- **THE PIT**: every running Claude Code session and Codex job, who is working, who is waiting on you, and a one-key way to jump to, answer, or hand off each one.
- **SESSIONS**: your projects with their running and finished conversations; click one and its real terminal opens beside the list.
- **QUEUE**: your task list, read straight out of an Obsidian vault (a folder of task notes plus a `.base` file for the tabs), or out of a plain Markdown folder if you don't use Obsidian.
- **BUDGET**: a usage and burn-rate HUD for your 5-hour and weekly limits, built only from local logs and Claude Code's status line (plus, optionally, your account's own usage reading).

Launching an agent on a task, starting a fresh session in a project, scheduling a start for later, and a pinned always-on assistant session all happen from the same deck. It was built for a desk monitor first and a phone over SSH second.

![Pantheon desk view, drawn from invented sample data](assets/deck.svg)

*The desk view, rendered from the test suite's invented sample projects.*

## Known issues

- **tmux freezes.** The tmux server has frozen under MSYS2 during window resizes and restarts. The cause is not known yet. One trigger that is known: moving a pane that runs a native Windows program (such as `claude.exe`) into another window with `join-pane` wedged the whole server once. The "stage a session beside the deck" feature is switched off for that reason.
- **Codex's interactive screen does not run under MSYS2** (upstream: openai/codex#6994, closed as not planned). Pantheon runs Codex headless only (`codex exec`), or opens interactive Codex in a separate native Windows Terminal window.
- **Built but never tested live:** the session-limit governor (winds sessions down before a limit and resumes them after the reset), the planning card, answering permission prompts from the deck, and the always-on assistant window. The governor and notifications ship switched off and in dry-run mode.
- **Snapshot tests**: the committed SVG snapshots were regenerated from the invented test data for this release. One phone-width check reads a render file that is not shipped, so it is skipped.
- **The launcher tests need the project's own venv.** `tests/test_launcher_tmux.py` runs `bin/pantheon` against a throwaway tmux server, and the launcher looks for `.venv/` inside the checkout. Without that venv, 3 of its tests fail.
- **Two helpers are not included.** The Codex-to-Claude hand-off router falls back to a separate `delegate-claude` script. The hand-off digest runs a `necromancy.ps1` session-digest script from `~/.claude/skills/`. Neither ships here. Without them, those two paths report that they could not run.
- **Hardcoded vocabulary.** The queue's tab rules follow one particular Sprints Base layout (the `next`, `agent`, `tier`, `status`, and `complexity` fields). Any Base with those fields works. A different schema needs `pantheon/queue/filters.py` edited.

## Requirements

- Windows 10/11 with [MSYS2](https://www.msys2.org/), plus MSYS2's `tmux` (`pacman -S tmux`).
- MSYS2's own Python 3.12, with a venv made from an MSYS2 shell. The deck and the tests run only under that venv, never under Git Bash or Windows Python.
- [Textual](https://textual.textualize.io/) and the other packages in `requirements.txt`.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) on PATH.
- Optional: the [Codex CLI](https://github.com/openai/codex), [ccusage](https://github.com/ryoppippi/ccusage) (through `npx`), Windows Terminal, an Obsidian vault.

## Install

From an MSYS2 shell:

```
git clone https://github.com/dtiger1889-ops/pantheon.git
cd pantheon
python -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Configure

Copy `pantheon.example.toml` to `pantheon.toml` and fill in the values marked REQUIRED. Or run `bin/pantheon` once with no `pantheon.toml` present: a first-run wizard asks for the three most important ones (vault folder, projects folder, tmux session name) and writes the file. `pantheon.toml` is gitignored. Every setting is documented in the example file.

To feed THE PIT and the BUDGET card, register these Claude Code hooks and status line in your `~/.claude/settings.json`:

- `hooks/pantheon_event.ps1` for SessionStart, PostToolUse, Stop, SessionEnd, Notification, and PermissionRequest
- `bin/statusline_capture` as the `statusLine` command
- optionally, `hooks/usage_line.py` as a `UserPromptSubmit` hook

Each file's header explains how to register it.

## Run

```
bin/pantheon              # start or attach the tmux session
bin/pantheon --keys       # print every key
bin/pantheon --restart    # restart the deck, queue and budget windows; agent windows keep running
```

Tests (from an MSYS2 shell; they never touch a live tmux server):

```
.venv/bin/python -m pytest -q --ignore=tests/test_snapshots.py
```

## Related

- [agent-deck-comparison](https://github.com/dtiger1889-ops/agent-deck-comparison): how Pantheon compares with other multi-agent terminal decks.

## License

MIT. See `LICENSE`.
