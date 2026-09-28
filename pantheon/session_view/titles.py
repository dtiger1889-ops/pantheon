"""Readable session titles for the sidebar and the resume list. Without it the sidebar
listed sessions as `Base directory for th…`, `# AGENTS.md instructi…`, raw UUIDs and
`[$spec](C:\\Users\\x\\…`.

Pure functions only -- no file reading here -- so every skip rule is unit-testable with one
hand-built record. `dispatch/sessions.title_of` (Claude transcripts) and `recent.py` (Codex
rollouts) do the bounded reading and call into this.

Order of preference, checked against real transcripts on this box:
1. the session's own title record: `custom-title` (`customTitle`, a `/rename` or the Desktop
   app's own name -- 39 of the 60 newest transcripts carry one), then `ai-title` (`aiTitle`),
   then an old-style `summary` record. The last one in the file wins.
2. the first REAL thing the user typed: not a skill body the harness injected ("Base directory
   for this skill"), not a `<command-...>` wrapper (the command and its args are used instead:
   `/orchestrate fix the deck`), not AGENTS.md / CLAUDE.md / environment injections, system
   reminders, hook output, tool results, compact summaries or image-only messages.
3. nothing -> the caller shows `untitled · <project> · <age>`, never a bare session id.
"""
from __future__ import annotations

import html
import re
from typing import Optional

TITLE_CHARS = 44   # a title + key + "15m ago" must fit one line of the 76-column Pick box

# How sure a first-user candidate is. A STRONG one ends the search; a WEAK one (a bare command,
# a scheduled-task name, "try again") is kept only if nothing strong follows.
STRONG = 2
WEAK = 1

# Text the harness put in a user turn. Matched case-insensitively against the start
# of the stripped text.
_INJECTED_PREFIXES = (
    "base directory for this skill",
    "caveat:",
    "stop hook feedback",
    "pretooluse",
    "posttooluse",
    "userpromptsubmit",
    "sessionstart",
    "[request interrupted",
    "this session is being continued from a previous conversation",
    "# agents.md instructions",
    "# claude.md",
    "contents of ",
    "the following is the codex agent history",
    "warmup",
    "[image:",
)

# Slash commands that set something up rather than say what the session is about.
_BORING_COMMANDS = {
    "clear", "model", "effort", "compact", "config", "status", "cost", "login", "logout",
    "resume", "mcp", "permissions", "context", "usage", "help", "rename", "exit", "quit",
    "fast", "output-style", "theme", "vim", "doctor", "memory", "hooks", "agents", "plugin",
    "plugins", "ide", "terminal-setup", "statusline", "add-dir", "release-notes", "export",
    "rewind", "bashes", "todos", "remote-control", "rc", "upgrade", "privacy-settings",
    "continue",
}

# Replies that only make sense mid-conversation ("try again") -- a title of last resort.
_WEAK_REPLIES = {
    "continue", "continue from where you left off", "try again", "go", "yes", "no", "ok",
    "okay", "done", "finish", "wrap", "proceed", "keep going", "retry",
}

_CMD_NAME = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>", re.S)
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_SCHEDULED = re.compile(r'<scheduled-task\s+name="([^"]+)"')
_SKILL_LINK = re.compile(r"\[\$([\w:-]+)\]\([^)]*\)")          # Codex: [$spec](C:\...\SKILL.md)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")               # [words](url) -> words
_AT_PATH = re.compile(r'@"([^"]+)"|@(\S+)')                     # @"C:\x\y.md" / @src/x.py
_URL = re.compile(r"https?://([^/\s]+)\S*")
# A path: anchored at a drive letter / `~` / `./` / a leading `/`, or relative with two or more
# separators, or one separator ending in a file extension. `sonnet/opus` and `and/or` are words,
# not paths, and are left alone.
_SEG = r"[^\s\\/\"'`()<>\[\]]"
_PATH = re.compile(
    rf"(?:[A-Za-z]:[\\/]+|~[\\/]+|\.{{1,2}}[\\/]+|(?<![\w.])/(?={_SEG}+/))(?:{_SEG}+[\\/]+)*{_SEG}*"
    rf"|{_SEG}+(?:[\\/]+{_SEG}+){{2,}}[\\/]*"
    rf"|{_SEG}+[\\/]+{_SEG}+\.[A-Za-z0-9]{{1,5}}\b"
)
_IMAGE_TOKEN = re.compile(r"\[Image #\d+\]", re.I)
_MD_NOISE = re.compile(r"(\*\*|__|`+|^#+\s*|^>\s*|^[-*]\s+)", re.M)
_MD_ESCAPE = re.compile(r"\\([_*\[\]()#>`~-])")
_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^>]*)?>")
# Worker / dispatch briefs open with a label or a constraint sentence the model wrote, not the
# subject: `User asks 2026-09-26: Dispatch an agent...`, `Read-only research for a spec, no
# edits. Research official Reddit API access...`.
_LEAD_LABEL = re.compile(
    r"^(?:(?:the\s+)?(?:user|the user)\s+(?:asks?|asked|request(?:s|ed)?|says|said|wants)"
    r"(?:\s*,?\s*\d{4}-\d{2}-\d{2})?|request(?:\s*,?\s*\d{4}-\d{2}-\d{2})?|task|context|goal|brief)"
    r"\s*:\s*",
    re.I,
)
_CONSTRAINT_SENTENCE = re.compile(
    r"^(?:read-only|read only|no edits|do not|don't|you are|you're|your worktree|important|"
    r"note|context|background)\b[^.!?\n]*[.!?\n]\s+",
    re.I,
)
_USE_SKILL = re.compile(r"^use the ([\w:-]+) skill (?:to\s+)?", re.I)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ---------------------------------------------------------------------------------- cleaning

def _path_words(match: re.Match) -> str:
    text = match.group(0).rstrip("\\/")
    last = re.split(r"[\\/]+", text)[-1]
    return last or text


def readable(text: str) -> str:
    """Markdown and paths down to plain words, whitespace collapsed. Never clips."""
    text = html.unescape(str(text or ""))
    text = _IMAGE_TOKEN.sub(" ", text)
    text = _SKILL_LINK.sub(lambda m: "/" + m.group(1), text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _AT_PATH.sub(lambda m: m.group(1) or m.group(2), text)
    text = _URL.sub(lambda m: m.group(1), text)
    text = _TAG.sub(" ", text)
    text = _MD_ESCAPE.sub(lambda m: m.group(1), text)
    text = _MD_NOISE.sub("", text)
    text = _PATH.sub(_path_words, text)
    text = " ".join(text.split())
    return text.strip(" \"'“”‘’:;,-")


def clip(text: str, limit: int = TITLE_CHARS) -> str:
    """Whitespace collapsed, cut at the last word boundary that fits, `…` appended when cut."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if text[len(cut)] != " ":          # the cut landed inside a word: back up to its start
        space = cut.rfind(" ")
        if space >= limit // 2:
            cut = cut[:space]
    return cut.rstrip(" ,.;:-–—") + "…"


def strip_preamble(text: str) -> str:
    """Drop a brief's opening label / constraint sentence when real words follow it."""
    for _ in range(3):
        before = text
        text = _LEAD_LABEL.sub("", text, count=1)
        m = _CONSTRAINT_SENTENCE.match(text)
        if m and len(text) > m.end() + 8:
            text = text[m.end():]
        text = _USE_SKILL.sub(lambda mm: "/" + mm.group(1) + " ", text, count=1)
        if text == before:
            break
    return text.strip()


def polish(text: str) -> str:
    """The full pipeline for a user-typed candidate: readable words, preamble dropped, and only
    its first line / first sentence-ish chunk when it is a long brief."""
    first = str(text or "").strip()
    # A long paste: the subject is on the first non-empty line.
    for line in first.splitlines():
        if line.strip():
            first = line if len(line.strip()) >= 12 else first
            break
    return strip_preamble(readable(first))


def is_uuid(text: Optional[str]) -> bool:
    return bool(text) and bool(_UUID.match(str(text).strip()))


# ---------------------------------------------------------------------------------- user records

def is_injected(text: str) -> bool:
    """True for text the harness put in a user turn rather than the user."""
    low = text.lstrip().lower()
    return any(low.startswith(p) for p in _INJECTED_PREFIXES)


def _message_text(content) -> Optional[str]:
    """The typed text of a message content value; None for tool results / image-only turns."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "tool_result":
            return None
        if kind in (None, "text", "input_text") and part.get("text"):
            texts.append(str(part["text"]))
    return "\n".join(texts) if texts else None


def text_candidate(text: Optional[str]) -> Optional[tuple[str, int]]:
    """(title, STRONG|WEAK) for one user-typed text, or None when it is not the user's words."""
    if not text:
        return None
    raw = str(text).strip()
    if not raw or is_injected(raw):
        return None
    if "<command-name>" in raw or "<command-message>" in raw:
        name = _CMD_NAME.search(raw)
        if not name:
            return None
        cmd = name.group(1).strip().lstrip("/")
        if cmd.lower() in _BORING_COMMANDS:
            return None
        args_m = _CMD_ARGS.search(raw)
        args = readable(args_m.group(1)) if args_m else ""
        title = f"/{cmd} {args}".strip()
        return (clip(title), STRONG if args else WEAK)
    sched = _SCHEDULED.match(raw)
    if sched:
        return (clip("scheduled: " + sched.group(1).replace("-", " ")), WEAK)
    if raw.startswith("<"):
        return None   # system reminders, hook output, local-command stdout, env context
    title = polish(raw)
    if not title or is_injected(title):
        return None
    words = title.rstrip(".!?").lower()
    strength = WEAK if (words in _WEAK_REPLIES or len(title) < 4) else STRONG
    return (clip(title), strength)


def user_record_candidate(rec: dict) -> Optional[tuple[str, int]]:
    """(title, strength) from one Claude transcript `user` record, or None to skip it."""
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return None
    if rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary"):
        return None
    if rec.get("toolUseResult") is not None:
        return None
    message = rec.get("message") or {}
    return text_candidate(_message_text(message.get("content")))


def title_record_value(rec: dict) -> Optional[tuple[str, int]]:
    """(title, rank) from a title-bearing record: 3 customTitle, 2 aiTitle, 1 summary."""
    if not isinstance(rec, dict):
        return None
    for key, rank in (("customTitle", 3), ("aiTitle", 2)):
        value = rec.get(key)
        if value and str(value).strip() and not is_uuid(value):
            return (clip(readable(value)), rank)
    if rec.get("type") == "summary" and rec.get("summary"):
        return (clip(readable(rec["summary"])), 1)
    return None


def codex_message_candidate(payload: dict) -> Optional[tuple[str, int]]:
    """(title, strength) from a Codex rollout `response_item` user message payload."""
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    return text_candidate(_message_text(payload.get("content")))


def untitled(project: Optional[str], age: Optional[str]) -> str:
    parts = ["untitled"]
    if project:
        parts.append(project)
    if age:
        parts.append(age)
    return " · ".join(parts)


def age_text(seconds: Optional[float]) -> Optional[str]:
    """`4m` / `3h` / `2d` -- the plain age words the sidebar already uses."""
    if seconds is None:
        return None
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"
