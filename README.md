# Pantheon

A terminal command deck for running several coding agents at once, from your desk or your phone. It shows every Claude Code and Codex session, who is waiting on you, how much of your usage window is left, and your task list, on one screen inside tmux.

![Pantheon desk view, drawn from invented sample data](assets/deck.svg)

## Why

When I run several agents in parallel on a subscription plan, the hard part is keeping track of which session needs an answer, how fast I'm burning through the 5-hour and weekly limits, and what to hand out next. My tasks already live in an Obsidian vault, so Pantheon reads them from there instead of asking me to keep a second tracker.

## Features

- **THE PIT**: a live table of every Claude Code session and Codex job with its state, project, last action and age. Sessions waiting on you are flagged from Claude Code's own hook events, not guessed from silence. Jump to, answer, kill, or hand off any session from its row.
- **Sessions**: your projects with their running and finished conversations. Start a new session with the model, effort and permission mode already set, or schedule one for later.
- **Queue**: your task list read straight from an Obsidian vault (task notes plus a `.base` file for the tabs), or from a plain Markdown folder. Pick a task and launch an agent on it with a briefing built from the note.
- **Budget**: 5-hour and weekly usage, burn rate, time to the limit and cost per day, built from local logs and Claude Code's status line. No reverse-engineered API calls.
- **Assistant**: an always-on session pinned in its own window, reachable from your phone through Claude's Remote Control.

## From your phone

Pantheon lives in tmux, so any SSH app on your phone (Termius, Blink, JuiceSSH) that reaches your PC, directly or over Tailscale, opens the same deck you left on your desk. It switches to a narrow layout that fits a phone screen, and there's no extra app, account or relay server to set up.

This is where it earns its keep for me. Starting agent work from a phone terminal usually means remembering or pasting a long `cd` plus `claude --model ... --effort ...` line on a phone keyboard. In Pantheon you press `n`, pick the project from a list, choose the model, effort and permission mode, type the first message, and it launches in its own tmux window. You can also start an agent on a task straight from your queue, answer a session that's waiting on you, or jump into any running session to take over.

<img src="assets/phone.svg" alt="Pantheon's phone layout, drawn from invented sample data" width="420">

## Requirements

- Windows 10/11 with [MSYS2](https://www.msys2.org/) and its `tmux` (`pacman -S tmux`)
- MSYS2's Python 3.12 (Git Bash and Windows Python are not supported)
- [Claude Code](https://docs.claude.com/en/docs/claude-code)
- Optional: [Codex CLI](https://github.com/openai/codex), [ccusage](https://github.com/ryoppippi/ccusage), Windows Terminal, an Obsidian vault

## Install

From an MSYS2 shell:

```
git clone https://github.com/dtiger1889-ops/pantheon.git
cd pantheon
python -m venv --copies .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Configure

Run `bin/pantheon` once and a first-run wizard asks for your vault folder, projects folder and tmux session name, then writes `pantheon.toml`. Every other setting is documented in `pantheon.example.toml`.

### Vault setup

`vault-template/` holds everything the queue needs in your Obsidian vault:

- `Projects/Sprints.base`: a Base with ten views for Obsidian itself. Pantheon's queue shows seven of them as tabs: Now, Decide, Quick wins, Agent's plate, Someday, Notes and By project.
- `Templates/Task.md`: a task note with every field the queue reads. Set it as your Templates folder's new-task template and save tasks to `Projects/Sprints/`.

The fields that drive the tabs:

| Field | Values | What it does |
|---|---|---|
| `summary` | text | The row's title |
| `project` | folder name | Groups By project; a launched agent starts in that folder under your projects root |
| `tier` | `now`, `soon`, `someday` | `someday` parks a row on Someday and keeps it off Decide and Quick wins |
| `status` | `open`, `in-progress`, `blocked`, `verify`, `done` | `blocked` rows go to Decide; `done` hides the row |
| `complexity` | `quick`, `moderate`, `heavy` | `quick` rows go to Quick wins |
| `agent` | `true`, `false` | `true` puts the row on Agent's plate |
| `est_context` | `small`, `medium`, `large` | Groups Agent's plate by how much work a session needs |
| `next` | `true` or empty | Pins the row to Now |
| `daytime` | `true`, `false` | Splits the Base's After hours and Business hours views (Obsidian only) |
| `done` | `true`, `false` | `true` hides the row everywhere |
| `note`, `reply` | text | A two-way message lane between you and the agent working the row |

If your vault uses a different folder, point `base_file` in `pantheon.toml` at your `.base`. No Obsidian? Set `task_source = "standalone"` and Pantheon reads a plain Markdown folder instead.

### Hooks

To feed THE PIT and the Budget card, register these in `~/.claude/settings.json` (each file's header shows how):

- `hooks/pantheon_event.ps1` as a hook for SessionStart, PostToolUse, Stop, SessionEnd, Notification and PermissionRequest
- `bin/statusline_capture` as the `statusLine` command

## Usage

```
bin/pantheon              # start or attach the deck
bin/pantheon --keys       # list every key
bin/pantheon --restart    # restart the deck windows; agent sessions keep running
```

Tests: `.venv/bin/python -m pytest -q`

## Known issues

- The tmux server has frozen a few times under MSYS2 during window resizes and restarts. The cause isn't known yet.
- Codex's interactive screen doesn't run under MSYS2 ([openai/codex#6994](https://github.com/openai/codex/issues/6994)), so Codex runs headless or in a separate Windows Terminal window.
- The limit governor, the planning card, answering permission prompts from the deck, and the assistant window are built but not yet tested in daily use. The governor ships switched off.
- Handing a session off to the other agent depends on two helper scripts that aren't included yet, so that path reports it couldn't run.
- The queue's tabs expect the task fields `next`, `agent`, `tier`, `status` and `complexity`. A different schema means editing `pantheon/queue/filters.py`.

## Related

- [agent-deck-comparison](https://github.com/dtiger1889-ops/agent-deck-comparison): Pantheon next to Orca, VelaTerm, Paseo and other agent managers.

## License

MIT. See `LICENSE`.
