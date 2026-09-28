"""Every key in the deck, in one place, in plain words.

`bin/pantheon --keys` prints this whole file's text; each pane's `?` overlay prints just its own
section. One line per key, description kept to about five words so it fits an 80-column phone
screen without wrapping.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

DECK = """\
deck (tmux window 0) - THE PIT: who is running, and what they need
  On a desk-sized screen the deck shows SESSIONS on the left - your projects, each with its
  sessions under it - and the table of every agent on the right. Click a session and its REAL
  terminal opens right beside the list, in this same window: type straight into it. The deck
  then shrinks to the list plus two lines of usage until you go back.

  the SESSIONS list on the left (click, or arrows + Enter)
    + New session   start a new session (same as n)
    Edit a file     open a file in a text editor right beside the list; it asks
                    which file, starting from the highlighted session's folder
                    (e does the same; the editor is micro, else nano - [editor]
                    command in pantheon.toml picks another)
    Assistant       your always-on Claude session in the workspace folder:
                    one long conversation, Dispatch without the Desktop app.
                    Opens beside the list; started first if not running.
                    From the phone: the Claude app's session list (Remote
                    Control). [assistant] in pantheon.toml turns it off
    a project name  a menu: new Claude session here, new Codex job here, resume a
                    conversation here, or open the folder in a shell window
    a running session   its terminal opens beside the list; click the list again to
                    pick another one (the one on screen goes back to its own window)
    a finished session  reopens it in a new window and opens that beside the list
                    (if it is still open in the Desktop app, it asks first)
    ...N more       the rest of that project's finished sessions
    < back          put the session on screen back in its own window (still running);
                    an open editor goes to its own EDIT window, your text kept
    1-9    open that running session, same as clicking it
    j      go to that agent's own full-screen window instead
    a d v  answer a permission prompt, same as in the table (below)
    Codex sessions that already finished cannot be reopened here (Codex's own screen
    does not run in tmux on this PC); start a new Codex job from the project menu

  the whole deck window
    t    show or hide your task list as a column on the right (F2 opens it in its own window)
    b    put the session on screen back in its own window (same as < back)
    E    edit a file beside the list (e in the SESSIONS list); pressed again, it
         goes back to the editor that is already open instead of opening another
    A    the Assistant beside the list (same as clicking its line)
    L    reconnect the Assistant's phone link when its line says
         `phone link off` (types /remote-control into it, only when it
         is idle at an empty prompt; same as the `↻ reconnect` line)
    n    start a new session: pick a project, who works it, an optional first message
         (or `pantheon open <project>` from a shell)
    P    the pit: tile every agent's real terminal side by side, in one tmux window
    ?    show this list of keys, then connectors, then tools (skills/hooks/guards)
    R    restart: the deck, queue and budget windows (agents keep
         running), or everything (asks twice) - `pantheon --restart`
         (a session open beside the list goes back to its own window first,
         and an open editor to its own EDIT window - it is never closed)
    q    quit this deck window only; your agents keep running

  the table of agents on the right, one row per agent
    j    jump to that agent's window
    a    allow once: a row asking permission shows what it wants, e.g.
         `!! publish · Bash · git push origin main`; a types the prompt's own
         plain "Yes" after checking the same prompt is still on screen (never
         "and don't ask again"); when the line ends `… +N` it shows all of it
         first, and a second a allows
    d    deny: the prompt's own "No"; Claude stops and asks what to do instead
    v    see the whole request word for word, the folder and the mode
         a question (AskUserQuestion), a plan, or a Desktop-app session is
         answered in its own window (j) or the Desktop app, never from here
    k    kill that window, asks first
    o    open the project folder here
    c    copy the session id out
    m    pick a model for that agent
    e    pick an effort level for it
    p    pick a permission mode for it
    h    hand this row to the other provider
    H    ask it to /checkpoint, then start a fresh session in its folder
    l    open a Codex job's log
    Enter  show every action for this row
    r    refresh the list right now
    where column: pantheon:3 = window 3 of this tmux; main:0 = another tmux;
    desktop = not in any tmux (Claude Desktop or a plain terminal); headless = a Codex job
    every key above is also a button under the selected row"""


QUEUE = """\
queue (tmux window 1) - the task list from the vault
  1-8  jump to a tab (the tabs are your Sprints Base's views, in its order:
       Now, Quick wins, Decide, My queue, Agent's plate, Someday, Notes, By project;
       "soon" is a tier on a row, not a tab -- soon rows sit in My queue and Agent's plate)
  ] [  next / previous tab
  Enter open the task detail (or, narrow, every action)
  Esc  go back one level
  /    search the tasks by text
  p    filter to one project
  w    work this task with claude now
  x    choose who works it (claude, codex, local)
  o    open this task's note (Obsidian, or an editor for a standalone folder)
  ?    show this list of keys
  R    restart the deck, queue and budget windows, or everything
  q    quit this queue window only; your agents keep running
  every key above is also a button under the highlighted row"""

HUD = """\
hud (tmux window 2) - usage, budget and burn rate
  r    refresh the usage numbers now
  ?    show this list of keys
  R    restart the deck, queue and budget windows, or everything
  q    quit this usage window only; your agents keep running"""

TOOLS = """\
tools (pantheon --tools, or ? from the deck) - slash skills, guards and hooks per
model, read-only, with a mark showing what Claude and Codex have in sync
  r    refresh now (these files change rarely; nothing here watches for changes)
  n    sort the skills list by name
  y    sort the skills list by sync state
  /    filter the skills list by name
  Esc  clear the filter
  ?    show this list of keys
  q    quit the tools window now
  fixing an out-of-sync skill is skill-sync's job, not this page's -- it never writes anything"""

SETTINGS = """\
settings (pantheon --settings) - font into Windows Terminal, colour palette
  Tab  move between fields
  Enter/Space  choose a face or a preset
  the FONT section is desk only (Windows Terminal reads its own settings.json); on the phone it
  shows a message instead of the controls
  Apply  writes the font into Windows Terminal now (backed up first); the palette applies live,
  no Apply needed
  ?    show this list of keys
  q    quit the settings window now"""

SECTIONS = {"deck": DECK, "supervisor": DECK, "queue": QUEUE, "hud": HUD, "tools": TOOLS,
            "appearance": SETTINGS}

ANYWHERE = """these keys work in every window, including inside an agent:
  F1   go to the deck window
  F2   go to the queue window
  F3   go to the usage window
  back_to_deck key (F12 by default; [keys] in pantheon.toml) also goes to the deck window --
       a second, separate way back, in case F12 or F1 does not reach you from your terminal

the panels:
  SESSIONS  your projects and their sessions    THE PIT  the table of every agent
  QUEUE     your task list, on the deck with t  BUDGET   usage and burn rate
At 120 columns and wider the deck shows SESSIONS beside THE PIT with BUDGET on top; narrower
(a phone over SSH) it shows the agents table alone, and F2 / F3 reach the task list and usage.
A session opened from SESSIONS on a phone-width screen switches you to its own window."""

KEYS_TEXT = "\n\n".join((ANYWHERE, DECK, QUEUE, HUD, TOOLS, SETTINGS))


def section(name: str) -> str:
    """The key list for one pane. An unknown name falls back to the whole list."""
    return SECTIONS.get((name or "").lower(), KEYS_TEXT)


def source_kind(cfg: "Config") -> str:
    """One line naming where the queue's rows come from right now, computed the same
    way `ObsidianBaseSource`/`StandaloneSource` compute their own `.kind` -- without instantiating
    either (no file reads, no folder watchers started) just to print a help screen."""
    if cfg.task_source == "standalone":
        folder = Path(cfg.standalone_settings().folder)
        return f"folder: {folder.name}"
    return f"Obsidian Base: {Path(cfg.sprints_base).stem}"
