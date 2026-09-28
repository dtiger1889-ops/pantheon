"""Permission prompts, answered from the deck.

The rule: a short line must never be a way to approve something that
was not shown. So this module is three plain pieces, all pure (no tmux, no clock, no files):

1. **What is being asked** (`pending`): the newest open permission request per session, from
   the event log. Claude Code's `PermissionRequest` hook (`hooks/pantheon_event.ps1`) carries
   the exact `tool_input`; until that hook entry is registered, only the `Notification` event
   ("Claude needs your permission", no tool, no command) arrives, and the window's own screen
   is the only other source (`read_screen`).
2. **The one-line summary** (`summarize` + `line`): `{marker} {risk} · {tool} · {target}`, at most 80
   characters, cut only at the end with `… +N` (N = characters not shown), never Claude's own
   description instead of the command.
3. **The answer** (`answer`): read the window, confirm the SAME prompt is still up, press the
   dialog's own key for plain "Yes" (never "Yes, and don't ask again") or "No", read again to
   confirm it closed. Any doubt sends nothing.

What the dialog looks like, and which key answers it, was read off live screens: the options are
numbered (`> 1. Yes` ... `4. No`), a digit answers at once with no Enter, `No` prints
"Interrupted · What should Claude do instead?" and runs nothing. AskUserQuestion draws a
different screen (a choice list, no "Do you want"), so it can never be mistaken for a yes/no.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from ..dialogs import _clean, dialog_region

WIDTH = 80

# The fixed list, heaviest first (the spec's table). The first four are the Warning tier.
RISKS = ("outside", "settings", "delete", "publish", "network", "run", "edit", "read", "trust",
         "question")
WARNING_RISKS = frozenset(("outside", "settings", "delete", "publish"))

# Tools whose answer is a choice or a plan, not a yes: shown in full, answered only in the window.
QUESTION_TOOLS = frozenset(("askuserquestion", "exitplanmode"))
EDIT_TOOLS = frozenset(("edit", "write", "multiedit", "notebookedit"))
READ_TOOLS = frozenset(("read", "glob", "grep", "ls", "notebookread"))
SHELL_TOOLS = frozenset(("bash", "powershell"))
NETWORK_TOOLS = frozenset(("webfetch", "websearch"))

# The events that mean the prompt is over (the spec's "Pending state" note). A `PostToolUse` only
# closes a prompt for the same tool: parallel tool calls can finish one while another still asks.
_ENDS_ALWAYS = frozenset(("stop", "sessionend", "sessionstart", "userpromptsubmit"))
_ENDS_SAME_TOOL = frozenset(("posttooluse", "posttoolusefailure", "pretooluse"))

# A Notification this soon after a PermissionRequest is the same prompt announcing itself (seen
# live: about six seconds apart), not a new one.
_SAME_PROMPT_SECONDS = 120


# --------------------------------------------------------------------------- the request


@dataclass(frozen=True)
class Request:
    """One open permission prompt for one session."""
    session_id: str
    tool: str = ""                        # Claude Code's name, "" when only a Notification came
    tool_input: Optional[dict] = None     # the exact request; None = the hook record is missing
    cwd: str = ""
    mode: str = ""                        # the permission mode the hook recorded
    since: Optional[str] = None           # ISO time the prompt first showed
    message: str = ""                     # the Notification's words, when one came
    subagent: bool = False

    @property
    def known(self) -> bool:
        """True when the full request (the hook's `tool_input`) is in hand."""
        return self.tool_input is not None


def _epoch(ts: Optional[str]) -> float:
    from ..models import parse_ts
    t = parse_ts(ts) if ts else None
    try:
        return t.timestamp() if t is not None else 0.0
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return 0.0


def pending(events: Iterable) -> dict[str, Request]:
    """session_id -> the open prompt, from the event log (oldest first is not required)."""
    by_sid: dict[str, list] = {}
    for e in events or []:
        sid = getattr(e, "session_id", None)
        if sid and (getattr(e, "source", None) or "claude").lower() == "claude":
            by_sid.setdefault(sid, []).append(e)
    out: dict[str, Request] = {}
    for sid, evs in by_sid.items():
        evs.sort(key=lambda e: (_epoch(e.ts), e.ts or ""))
        cur: Optional[Request] = None
        for e in evs:
            name = (e.event or "").lower()
            extra = getattr(e, "extra", None) or {}
            if name == "permissionrequest":
                ti = extra.get("tool_input")
                cur = Request(sid, e.tool_name or "", ti if isinstance(ti, dict) else None,
                              e.cwd or "", str(extra.get("permission_mode") or ""), e.ts, "",
                              bool(e.agent_id))
            elif name == "notification" and (e.notification_type or "").lower() == "permission_prompt":
                msg = (e.message or "").strip()
                if cur is not None and _epoch(e.ts) - _epoch(cur.since) <= _SAME_PROMPT_SECONDS:
                    cur = Request(cur.session_id, cur.tool, cur.tool_input, cur.cwd or e.cwd or "",
                                  cur.mode, cur.since, msg, cur.subagent)
                else:
                    cur = Request(sid, e.tool_name or _tool_from_message(msg), None, e.cwd or "",
                                  str(extra.get("permission_mode") or ""), e.ts, msg, bool(e.agent_id))
            elif cur is not None and name in _ENDS_ALWAYS and not e.agent_id:
                cur = None
            elif cur is not None and name in _ENDS_SAME_TOOL:
                if not cur.tool or (e.tool_name or "").lower() == cur.tool.lower():
                    cur = None
        if cur is not None:
            out[sid] = cur
    return out


def _tool_from_message(msg: str) -> str:
    """`Claude needs your permission to use Bash` -> `Bash` (older Claude Code builds said so)."""
    m = re.search(r"permission to use (\S+)", msg or "")
    return m.group(1).rstrip(".") if m else ""


# --------------------------------------------------------------------------- paths


def _norm(path: str) -> str:
    """One comparable form: forward slashes, lower case, `/c/x` -> `c:/x`, no trailing slash."""
    p = (path or "").strip().strip("'\"").replace("\\", "/")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        p = f"{m.group(1)}:/{m.group(2)}"
    return p.rstrip("/").lower()


def _is_abs(path: str) -> bool:
    p = (path or "").strip().strip("'\"")
    return bool(re.match(r"^([a-zA-Z]:[\\/]|/|~)", p))


def inside(path: str, cwd: str) -> bool:
    """Is `path` in the session's folder? A relative path is (it resolves against the folder);
    `..` anywhere is not."""
    if not path:
        return True
    raw = path.strip().strip("'\"").replace("\\", "/")
    if ".." in raw.split("/"):
        return False
    if not _is_abs(raw):
        return True
    if raw.startswith("~") or not cwd:
        return False
    p, c = _norm(raw), _norm(cwd)
    return p == c or p.startswith(c + "/")


def shown_path(path: str, cwd: str) -> str:
    """Relative to the session's folder when inside it, the full path when outside."""
    raw = (path or "").strip().replace("\\", "/")
    if cwd and _is_abs(raw) and inside(raw, cwd):
        c = cwd.replace("\\", "/").rstrip("/")
        rel = raw[len(c):].lstrip("/") if raw.lower().startswith(c.lower()) else raw
        return rel or "."
    return raw


_SETTINGS_RE = re.compile(
    r"(^|[\s/\"'=])(settings[^/\s\"']*\.json|claude\.md|checkpoint\.md)\b"
    r"|(^|[\s/\"'])hooks/|(^|[\s/\"'])\.git(/|\s|$)|(^|[\s/\"'~])\.claude", re.I)


def touches_settings(text: str) -> bool:
    return bool(_SETTINGS_RE.search((text or "").replace("\\", "/")))


# --------------------------------------------------------------------------- commands


def split_commands(command: str) -> list[str]:
    """`cd app && npm ci; make` -> three parts. Splits on `&&`, `||`, `;` and new lines outside
    quotes; a pipe stays one part (it is one pipeline)."""
    parts, buf, quote, i = [], [], "", 0
    s = command or ""
    while i < len(s):
        ch = s[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = s[i:i + 2]
        if two in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";\n":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _words(part: str) -> list[str]:
    return re.findall(r"\"[^\"]*\"|'[^']*'|\S+", part)


_DELETE_RE = re.compile(
    r"(^|[\s;&|(])(rm|del|erase|rmdir|rd|remove-item|ri|shred|unlink)(\s|$)"
    r"|git\s+clean\b|git\s+reset\s+[^;&|]*--hard|--force\b|(^|\s)-rf?\b|(^|\s)-fr\b", re.I)
_PUBLISH_RE = re.compile(
    r"git\s+push\b|npm\s+publish\b|pnpm\s+publish\b|yarn\s+publish\b|twine\s+upload\b"
    r"|cargo\s+publish\b|(^|[\s;&|])scp\s|curl\b[^;&|]*(\s-T\s|--upload-file|\s-F\s|--form\b"
    r"|\s-X\s*(post|put|patch|delete)\b)|invoke-(webrequest|restmethod)\b[^;&|]*-method\s+(post|put|patch|delete)",
    re.I)
_GH_READ = frozenset(("view", "list", "status", "diff", "checks", "--help", "help", "search",
                      "browse", "auth"))
_NETWORK_RE = re.compile(
    r"(^|[\s;&|(])(curl|wget|ssh|scp|sftp|rsync|nc|ncat|telnet|ftp|invoke-webrequest|iwr"
    r"|invoke-restmethod|irm|git\s+(clone|fetch|pull|ls-remote)|gh)(\s|$)", re.I)


def _gh_writes(command: str) -> bool:
    for part in split_commands(command):
        words = [w.lower() for w in _words(part)]
        for i, w in enumerate(words):
            if w in ("gh", "gh.exe"):
                rest = words[i + 1:i + 3]
                if rest and not any(r in _GH_READ for r in rest):
                    if not (rest[0] == "api" and not any(x in part.lower() for x in
                                                         ("-x post", "-x put", "-x patch", "-x delete",
                                                          "--method post", "--method put", "-f ", "--field"))):
                        return True
    return False


def _command_leaves_folder(command: str, cwd: str) -> bool:
    """A `cd` out of the folder, or an absolute path outside it (the program's own path and
    `/dev/null` do not count)."""
    for part in split_commands(command):
        words = _words(part)
        if not words:
            continue
        if words[0].lower() in ("cd", "pushd", "set-location", "sl", "chdir"):
            if len(words) < 2 or not inside(words[1], cwd):
                return True
            continue
        for w in words[1:]:
            w = w.strip("'\"")
            w = re.sub(r"^[0-9]?[<>]+", "", w)         # `>/tmp/x`, `2>/dev/null`
            if "=" in w and not _is_abs(w):
                w = w.split("=", 1)[1]                 # `--out=/tmp/x`
            if not _is_abs(w) or w.startswith("-"):
                continue
            if _norm(w).startswith("/dev/") or _norm(w) in ("nul", "/dev/null"):
                continue
            if not inside(w, cwd):
                return True
    return False


def risk(tool: str, tool_input: Optional[dict], cwd: str = "") -> str:
    """The one risk word: the heaviest that applies (`RISKS`)."""
    t = (tool or "").lower()
    ti = tool_input or {}
    if t in QUESTION_TOOLS:
        return "question"
    if t in SHELL_TOOLS:
        cmd = str(ti.get("command") or "")
        if _command_leaves_folder(cmd, cwd):
            return "outside"
        if touches_settings(cmd):
            return "settings"
        if _DELETE_RE.search(cmd):
            return "delete"
        if _PUBLISH_RE.search(cmd) or _gh_writes(cmd):
            return "publish"
        if _NETWORK_RE.search(cmd):
            return "network"
        return "run"
    path = str(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")
    if t in EDIT_TOOLS or t in READ_TOOLS:
        if path and not inside(path, cwd):
            return "outside"
        if touches_settings(path):
            return "settings"
        return "edit" if t in EDIT_TOOLS else "read"
    if t in NETWORK_TOOLS or t.startswith("mcp__"):
        return "network"
    if path and not inside(path, cwd):
        return "outside"
    return "run"


# --------------------------------------------------------------------------- the summary line


def tool_label(tool: str) -> str:
    """`mcp__github__create_issue` -> `create_issue`; everything else as Claude Code names it."""
    if (tool or "").startswith("mcp__"):
        return tool.split("__")[-1] or tool
    return tool or "?"


def _first_value(ti: dict) -> str:
    for v in ti.values():
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
    return json.dumps(ti, ensure_ascii=False) if ti else ""


def target(tool: str, tool_input: Optional[dict], cwd: str = "", ascii_only: bool = False) -> str:
    """What the request is about, word for word (never Claude's description of it)."""
    t = (tool or "").lower()
    ti = tool_input or {}
    if t in SHELL_TOOLS:
        cmd = str(ti.get("command") or "").strip()
        nl = " \\n " if ascii_only else " ⏎ "
        flat = nl.join(line.rstrip() for line in cmd.splitlines())
        parts = split_commands(cmd)
        if len(parts) > 1:
            return f"{len(parts)} commands: {flat}"
        return flat
    if t == "askuserquestion":
        qs = ti.get("questions") or []
        if qs and isinstance(qs[0], dict):
            return str(qs[0].get("question") or "")
        return ""
    if t == "exitplanmode":
        return "approve the plan"
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if path and (t in EDIT_TOOLS or t in READ_TOOLS or not t.startswith("mcp__")):
        shown = shown_path(str(path), cwd)
        if t in ("glob", "grep") and ti.get("pattern"):
            return f"{ti.get('pattern')} in {shown}"
        return shown
    if t in ("glob", "grep"):
        return str(ti.get("pattern") or "")
    if t == "webfetch":
        url = str(ti.get("url") or "")
        return urlparse(url).netloc or url
    if t == "websearch":
        return str(ti.get("query") or "")
    return _first_value(ti)


@dataclass(frozen=True)
class Summary:
    risk: str
    tool: str
    target: str
    known: bool          # False = built from the screen or a bare Notification, not the hook

    @property
    def warning(self) -> bool:
        return self.risk in WARNING_RISKS

    def marker(self, ascii_only: bool = False) -> str:
        if self.warning:
            return "!!"
        return "!" if ascii_only else "▲"


def summarize(req: Request, screen: Optional["ScreenPrompt"] = None,
              ascii_only: bool = False) -> Summary:
    """The request in its three parts. The hook's exact `tool_input` wins; without it, the
    window's own screen; without that, only the tool name is known."""
    if req.known:
        return Summary(risk(req.tool, req.tool_input, req.cwd), tool_label(req.tool),
                       target(req.tool, req.tool_input, req.cwd, ascii_only), True)
    if screen is not None:
        tool = screen.tool or req.tool
        if screen.kind == "question":
            return Summary("question", tool_label(tool or "question"), screen.target, False)
        body_text = " ".join(screen.body)
        ti = {"command": body_text} if tool.lower() in SHELL_TOOLS else {"file_path": screen.target}
        word = risk(tool, ti, req.cwd)
        return Summary(word, tool_label(tool or "?"), screen.target, False)
    return Summary("question" if req.tool.lower() in QUESTION_TOOLS else
                   ("run" if not req.tool else risk(req.tool, None, req.cwd)),
                   tool_label(req.tool) if req.tool else "?",
                   "full request not known", False)


def clip(text: str, width: int, ascii_only: bool = False) -> tuple[str, int]:
    """`text` cut to `width` characters ending `… +N` (N = characters not shown); (text, 0) when
    it fits. N counts from the end of `text`, so nothing is ever silently dropped."""
    if width <= 0:
        return "", len(text)
    if len(text) <= width:
        return text, 0
    ell = "..." if ascii_only else "…"
    keep = width
    while keep > 0:
        hidden = len(text) - keep
        suffix = f"{ell} +{hidden}"
        if keep + len(suffix) <= width:
            return text[:keep] + suffix, hidden
        keep -= 1
    return text[:width], len(text) - width


def line(s: Summary, width: int = WIDTH, ascii_only: bool = False) -> tuple[str, int]:
    """`!! publish · Bash · git push origin main`, at most `width` characters. Returns the line
    and how many characters of it were cut (0 = the whole request is on the line)."""
    dot = " - " if ascii_only else " · "
    text = f"{s.marker(ascii_only)} {s.risk}{dot}{s.tool}{dot}{s.target}"
    if ascii_only:
        text = text.encode("ascii", "replace").decode("ascii")
    return clip(text, width, ascii_only)


def needs_full_view(s: Summary, cut: int) -> bool:
    """The first `a` opens the full view instead of allowing: the line was cut, or the request
    was not read word for word from Claude Code's own record."""
    return cut > 0 or not s.known


# --------------------------------------------------------------------------- the screen


_OPTION_RE = re.compile(r"^(?:[>❯›]\s*)?(\d+)\.\s+(.*)$")
_TITLE_TOOLS = (("bash command", "Bash"), ("powershell command", "PowerShell"),
                ("create file", "Write"), ("overwrite file", "Write"), ("edit file", "Edit"),
                ("read file", "Read"), ("fetch", "WebFetch"), ("web search", "WebSearch"),
                ("notebook", "NotebookEdit"))


@dataclass(frozen=True)
class ScreenPrompt:
    """A dialog read off a window's screen."""
    kind: str                 # "permission" (Yes/No) | "question" (a choice list)
    title: str                # "Bash command", "Create file", "" when none
    tool: str                 # the tool that title names, "" when unknown
    body: tuple = ()          # the request as drawn (command lines, path, description)
    question: str = ""        # "Do you want to proceed?"
    options: tuple = ()       # ((number, text), ...)
    selected: int = -1
    yes_key: str = ""         # the digit of the plain "Yes" option
    no_key: str = ""          # the digit of the "No" option
    text: str = ""            # the whole dialog, cleaned, for the full view

    @property
    def target(self) -> str:
        if self.kind == "question":
            return self.question or (self.body[0] if self.body else "")
        return self.body[0] if self.body else self.title

    @property
    def flat(self) -> str:
        """The dialog's text with ALL white space removed -- wrapping-proof comparisons."""
        return re.sub(r"\s+", "", self.text)


def _screen_lines(screen: str) -> list[str]:
    return [_clean(l).lstrip("│").strip() if l.strip().startswith("│") else _clean(l)
            for l in dialog_region(screen)]


def read_screen(screen: str) -> Optional[ScreenPrompt]:
    """The permission dialog or choice list at the bottom of this screen, or None."""
    region = dialog_region(screen)
    lines = [re.sub(r"^\s*│\s?", "", l).rstrip() for l in region]
    cleaned = [_clean(l) for l in lines]
    opts: list[tuple[str, str]] = []
    selected = -1
    first_opt = None
    for i, l in enumerate(cleaned):
        m = _OPTION_RE.match(l)
        if m:
            if first_opt is None:
                first_opt = i
            if l[:1] in ">❯›":
                selected = len(opts)
            opts.append((m.group(1), m.group(2).strip()))
    flat_all = " ".join(l.lower() for l in cleaned if l)
    if not opts:
        return None
    texts = [t.lower() for _n, t in opts]
    yes_key = next((n for n, t in opts if t.strip().lower() == "yes"), "")
    no_key = next((n for n, t in opts if t.strip().lower() in ("no", "no, exit")
                   or t.lower().startswith("no,") or t.lower().startswith("no ")), "")
    question_idx = next((i for i, l in enumerate(cleaned[:first_opt])
                         if l.lower().startswith("do you want")), None)
    text = "\n".join(cleaned).strip("\n")
    if question_idx is not None and yes_key and no_key:
        head = [l for l in cleaned[:question_idx]]
        nonblank = [i for i, l in enumerate(head) if l]
        title = head[nonblank[0]] if nonblank else ""
        tool = next((t for k, t in _TITLE_TOOLS if title.lower().startswith(k)), "")
        body = []
        started = False
        for l in head[(nonblank[0] + 1) if nonblank else 0:]:
            if l.lower().startswith("tip:"):
                continue
            if not l:
                if started:
                    break
                continue
            if not l.strip("╌─━═-_ "):
                continue            # the dashed frame a file dialog draws round its preview
            started = True
            body.append(l)
        return ScreenPrompt("permission", title, tool, tuple(body), cleaned[question_idx],
                            tuple(opts), selected, yes_key, no_key, text)
    if ("enter to select" in flat_all or "type something" in " ".join(texts)
            or "chat about this" in " ".join(texts)):
        above = [l for l in cleaned[:first_opt] if l]
        q = next((l for l in reversed(above) if l.endswith("?")), above[-1] if above else "")
        return ScreenPrompt("question", "", "AskUserQuestion", tuple(above), q, tuple(opts),
                            selected, "", "", text)
    return None


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def matches(prompt: ScreenPrompt, req: Request, shown_text: str = "") -> bool:
    """Is the dialog on screen the request the deck showed? Wrapping-proof: all white space is
    removed from both sides before comparing.

    - known Bash/PowerShell: the command's first 120 characters are in the dialog;
    - known file tools: the file's name is in the dialog and it is not a command dialog;
    - other known tools: the tool's short name or its target is in the dialog;
    - a request known only from the screen (`shown_text`): the dialog reads exactly the same."""
    if prompt.kind != "permission":
        return False
    flat = _squash(prompt.text)
    if not req.known:
        return bool(shown_text) and _squash(shown_text) == flat
    t = (req.tool or "").lower()
    ti = req.tool_input or {}
    if t in SHELL_TOOLS:
        if "command" not in prompt.title.lower():
            return False
        cmd = _squash(str(ti.get("command") or ""))
        return bool(cmd) and cmd[:120] in flat
    path = str(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")
    if path:
        name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return bool(name) and _squash(name) in flat and "command" not in prompt.title.lower()
    for needle in (tool_label(req.tool), target(req.tool, ti, req.cwd)):
        if needle and _squash(needle)[:60] in flat:
            return True
    return False


# --------------------------------------------------------------------------- answering

PROMPT_CHANGED = "the prompt changed -- j to answer it there"
ANSWER_THERE = "j to answer it there"
ANSWER_IN_DESKTOP = "answer it in the Desktop app"
ANSWERED_ELSEWHERE = "already answered in the window"


def answer(target_pane: str, req: Request, allow: bool,
           capture: Callable[[str], str], press: Callable[[str, str], bool],
           sleep: Callable[[float], None] = lambda s: None,
           shown_text: str = "", settle: float = 0.6) -> tuple[bool, str]:
    """Answer one prompt in one window with one key, only after a fresh read shows it is still
    the same prompt. Never presses anything but the plain "Yes" or the "No" option's digit."""
    screen = capture(target_pane)
    if not screen or not screen.strip():
        return False, "could not read that window -- j to look at it"
    prompt = read_screen(screen)
    if prompt is None:
        return False, ANSWERED_ELSEWHERE
    if prompt.kind == "question" or req.tool.lower() in QUESTION_TOOLS:
        return False, f"that is a question, not a yes/no -- {ANSWER_THERE}"
    if not matches(prompt, req, shown_text):
        return False, PROMPT_CHANGED
    key = prompt.yes_key if allow else prompt.no_key
    if not key:
        return False, PROMPT_CHANGED
    press(target_pane, key)
    sleep(settle)
    after = read_screen(capture(target_pane))
    if after is not None and after.kind == "permission" and after.text == prompt.text:
        return False, "pressed it, but the prompt is still on screen -- j to look"
    return True, "allowed once" if allow else "denied; Claude will ask what to do instead"


def busy(screen: str) -> bool:
    """Claude is working (its spinner line says so), as opposed to waiting at the prompt."""
    tail = "\n".join((screen or "").splitlines()[-25:]).lower()
    return "esc to interrupt" in tail


# --------------------------------------------------------------------------- the full view


def full_text(req: Request, screen: Optional[ScreenPrompt], project: str, where: str,
              waited: str) -> str:
    """Everything `v` shows, word for word. The hook's record when there is one; otherwise the
    dialog exactly as the window draws it, and a plain note that the hook's record is missing."""
    lines: list[str] = []
    tool = req.tool or (screen.tool if screen else "") or "a tool"
    lines.append(f"Claude wants to use {tool_label(tool)} in {req.cwd or project or '?'}")
    lines.append("")
    if req.known:
        ti = dict(req.tool_input or {})
        said = ti.pop("description", None)
        t = (req.tool or "").lower()
        if t in SHELL_TOOLS:
            lines.extend("    " + l for l in str(ti.pop("command", "")).splitlines() or [""])
        elif "file_path" in ti:
            lines.append("    " + str(ti.pop("file_path")))
        rest = {k: v for k, v in ti.items()}
        if rest:
            lines.append("")
            for k, v in rest.items():
                if isinstance(v, str):
                    body = v.splitlines() or [""]
                    lines.append(f"  {k}:")
                    lines.extend("    " + l for l in body)
                else:
                    lines.append(f"  {k}:")
                    lines.extend("    " + l for l in json.dumps(v, indent=2, ensure_ascii=False).splitlines())
        if said:
            lines.append("")
            lines.append(f"Claude says: {said}")
    else:
        lines.append("Pantheon's PermissionRequest hook is not registered, so the exact request")
        lines.append("is not known; this is the prompt as the window shows it:")
        lines.append("")
        if screen is not None:
            lines.extend("    " + l for l in screen.text.splitlines())
        else:
            lines.append("    " + (req.message or "Claude needs your permission"))
    lines.append("")
    lines.append(f"waiting {waited} · permission mode {req.mode or '?'} · {where}")
    return "\n".join(lines)
