"""Shared seam for the condensed session view.

Three packages build against these types in parallel, so this file changes only with a
commit message that says why, and never in a way that drops a field:

- `transcript.py` (parser) PRODUCES `Conversation` from a Claude Code transcript file
  (`~/.claude/projects/<slug>/<session_id>.jsonl`), incrementally (append-only file).
- `recent.py` (sidebar data) PRODUCES `SessionEntry` rows: every live agent THE PIT knows,
  plus recent finished transcripts across projects.
- `conversation.py` (widget) CONSUMES both and draws them.

Everything is plain data (frozen dataclasses, str/int/bool/None) so a test can build any
shape by hand without a transcript on disk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------- one item of a conversation

# `Item.kind` values. The widget draws each kind differently; the parser emits nothing else.
USER = "user"            # something the user typed (or a queued prompt that was delivered)
ASSISTANT = "assistant"  # the assistant's prose for one turn segment (Markdown source)
THINKING = "thinking"    # a thinking block -- drawn folded, expandable
TOOL = "tool"            # one tool call, with its result once it arrived -- drawn as a chip
SYSTEM = "system"        # hook receipts, system reminders, bridge/remote-control notes, command
                         # caveats -- drawn as a dim folded one-liner
SUBAGENT = "subagent"    # a sidechain (an Agent tool's own turns) rolled into one folded item

KINDS = (USER, ASSISTANT, THINKING, TOOL, SYSTEM, SUBAGENT)

# Caps the parser applies so a 10 MB transcript never becomes a 10 MB widget tree. The widget
# may show less; it never has to truncate itself.
INPUT_CHARS = 2_000      # a tool call's pretty-printed input
RESULT_CHARS = 4_000     # a tool result
TEXT_CHARS = 40_000      # one assistant/user/thinking text
CHIP_CHARS = 72          # one-line chip summary, e.g. `Read CHECKPOINT.md`


@dataclass(frozen=True)
class ToolCall:
    """One tool_use block and, once the matching tool_result has been read, its outcome."""
    id: str                       # the `toolu_...` id (joins the result to the call)
    name: str                     # "Read", "Bash", "Agent", "mcp__x__y", ...
    summary: str                  # the chip line, <= CHIP_CHARS: `Read CHECKPOINT.md`,
                                  # `Bash git status --short`, `Agent: fix the launcher`
    input_text: str = ""          # pretty input for the expanded chip, <= INPUT_CHARS
    result_text: Optional[str] = None   # None until the result record arrives; <= RESULT_CHARS
    is_error: bool = False        # the tool_result's `is_error`
    file: Optional[str] = None    # basename of the file the tool touched, when it names one

    @property
    def done(self) -> bool:
        return self.result_text is not None


@dataclass(frozen=True)
class Item:
    """One drawable piece of the conversation, in transcript order."""
    kind: str                     # one of KINDS
    text: str = ""                # USER/ASSISTANT/THINKING: the text (Markdown for ASSISTANT);
                                  # SYSTEM: the one-liner; SUBAGENT: a one-line summary
    ts: Optional[str] = None      # the record's ISO timestamp, when it had one
    uuid: Optional[str] = None    # the record's uuid (stable widget ids; None for synthetic items)
    tool: Optional[ToolCall] = None       # TOOL only
    detail: Optional[str] = None  # SYSTEM/SUBAGENT: the folded body (full hook output, the
                                  # subagent's own items flattened to text)
    sidechain: bool = False       # True for records with `isSidechain`; the parser folds
                                  # runs of these into one SUBAGENT item and sets this on it


@dataclass(frozen=True)
class Conversation:
    """A whole session as read so far. Immutable; `transcript.update()` returns a new one."""
    session_id: str
    path: str                     # the transcript file
    provider: str = "claude"      # "claude" (~/.claude/projects) | "codex" (~/.codex/sessions)
    items: tuple = ()             # tuple[Item, ...] in transcript order
    title: Optional[str] = None   # `customTitle` > last `aiTitle` > first user line
    model: Optional[str] = None   # `message.model` of the latest assistant record
    effort: Optional[str] = None  # top-level `effort` of the latest assistant record
    context_tokens: int = 0       # the latest assistant turn's usage total (input + output +
                                  # both cache buckets) -- the "current context size" number
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    entrypoint: Optional[str] = None   # "cli" | "claude-desktop" | ... from the records
    offset: int = 0               # bytes of the file consumed; `update()` resumes here
    pending: tuple = ()           # tuple[str, ...]: tool ids still awaiting a result
    parse_errors: int = 0
    title_rank: int = 0           # parser bookkeeping so `update` matches a fresh `load`:
                                  # 0 none, 1 first-user-line, 2 last aiTitle, 3 customTitle --
                                  # a customTitle must outrank every later aiTitle even when it
                                  # was read in an earlier `update` batch
    sidechain_pending: tuple = ()  # tuple[str, ...]: flattened text from an isSidechain run
                                   # still in progress -- not yet folded into a SUBAGENT item,
                                   # because the run may continue in the next `update` batch
                                   #
    last_commit_at: Optional[str] = None   # ISO ts of the latest git commit whose tool call came
    last_push_at: Optional[str] = None     # back without an error / same for git push

    @property
    def last_item(self) -> Optional[Item]:
        return self.items[-1] if self.items else None


# ---------------------------------------------------------------- the sidebar's rows

LIVE = "live"          # a running agent THE PIT lists (state from `supervisor/state.fold`)
RECENT = "recent"      # a finished transcript, read-only, resume through `n`


@dataclass(frozen=True)
class SessionEntry:
    """One sidebar row. Live rows come from `AgentState`; recent rows from transcript files."""
    session_id: str
    group: str                    # LIVE | RECENT
    project: str                  # folder name under projects_root, or the cwd's basename
    cwd: str
    title: str                    # the transcript title (SessionInfo.title rules), else project
    provider: str = "claude"      # "claude" | "codex" | ...
    status_text: str = ""         # LIVE: the state label THE PIT shows ("working", "needs you")
    style: Optional[str] = None   # LIVE: `supervisor/pane.row_style()` -- the theme token name
    needs_human: bool = False
    window_index: Optional[int] = None   # LIVE and in the pantheon tmux: where `j` goes and
                                         # where the message box types; None = read-only
    tmux_session: Optional[str] = None
    transcript_path: Optional[str] = None   # None when no transcript exists (a Codex job)
    modified_ts: Optional[str] = None       # ISO, the transcript's mtime (sorting recents)
    last_commit_at: Optional[str] = None    # ISO, the session's latest successful git commit
    last_push_at: Optional[str] = None      # ISO, its latest successful git push -- both from
                                            # the transcript (`gitmarks.py`), None = never seen;
                                            # `gitmarks.stop_point_text(entry)` words them
                                            #

    @property
    def can_type(self) -> bool:
        """True when a message typed in the deck can reach this session's terminal."""
        return self.group == LIVE and self.window_index is not None

    @property
    def target(self) -> Optional[str]:
        """The tmux target the message box types into (`pantheon:3`), or None."""
        if not self.can_type:
            return None
        return f"{self.tmux_session or 'pantheon'}:{self.window_index}"
