"""The trade-off button: hand a dead or parked session's work to the other provider
.

Three functions, in the order the deck calls them:
  `digest_for(row, cfg)`         -- what the dead/parked agent was doing, capped and clipped.
  `handoff_briefing(...)`        -- the new agent's first prompt: the row's own briefing (if it
                                     came from the queue) plus the digest and a hand-off note.
  `handoff(row, to_provider, ...)` -- runs the normal guard + dispatch with that briefing, then
                                     logs a `handoff` event and (for a parked row) marks it
                                     resumed so the governor never also resumes it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from .. import events as events_mod
from ..models import AgentState, AgentStatus, Event, LaunchResult, TaskRow, format_age, utcnow_iso
from ..dispatch import briefing as briefing_mod
from ..dispatch import launch as launch_mod
from . import parked as parked_mod

NOT_FOUND_SENTENCE = "no transcript was found for the previous agent; start from the row and CHECKPOINT.md"
MAX_DIGEST_LINES = 60
MAX_LINE_CHARS = 200
CODEX_LOG_TAIL_LINES = 80

# The necromancy digest's own section headings (from the session-digest script)
# that section 11 names: FIRST ASK, USER MESSAGES, FINAL ASSISTANT STATE, FILES WRITTEN/READ,
# COMMANDS, GIT/COMMITS, ERRORS. Its PRS / VERSIONS SEEN / TEST RESULTS sections are dropped --
# not useful context for picking work back up.
KEEP_SECTION_PREFIXES = (
    "FIRST ASK", "USER MESSAGES", "FINAL ASSISTANT STATE",
    "FILES WRITTEN", "FILES READ", "COMMANDS RUN", "GIT / COMMITS", "ERRORS",
)

REASON_WORDS = {
    "gone": "it was killed or its window closed",
    "failed": "it hit an error",
    "parked": "it was parked by the governor",
    "resume-failed": "the governor could not resume it",
    "winding-down": "a usage limit was closing in",
    "done": "it finished its last job",
}


def _necromancy_script(cfg) -> Path:
    return Path(cfg.claude_home) / "skills" / "necromancy" / "necromancy.ps1"


def _cap(text: str, max_lines: int = MAX_DIGEST_LINES, max_line_chars: int = MAX_LINE_CHARS) -> str:
    """The necromancy digest's cap: 60 lines total, 200 characters per
    line, kept from the TOP (the digest is already ordered oldest-first-ask to newest-error)."""
    lines = [line[:max_line_chars] for line in text.splitlines()]
    return "\n".join(lines[:max_lines]).rstrip("\n")


def _cap_line_length(text: str, max_line_chars: int = MAX_LINE_CHARS) -> str:
    """Just the per-line length cap, keeping every line -- used for the Codex log tail, which
    is already the last `CODEX_LOG_TAIL_LINES` and must stay the last ones, not be re-cut from
    the top the way the necromancy digest is."""
    return "\n".join(line[:max_line_chars] for line in text.splitlines())


def _filter_necromancy_sections(raw: str) -> str:
    """Keep the header block (before the first `== ... ==` heading) plus only the named
    sections, heading included so FILES WRITTEN is never confused with FILES READ; drop
    PRS / VERSIONS SEEN / TEST RESULTS (heading and content) entirely."""
    out: list[str] = []
    keep = True
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("==") and stripped.endswith("==") and len(stripped) > 4:
            heading = stripped.strip("= ").strip().upper()
            keep = any(heading.startswith(prefix) for prefix in KEEP_SECTION_PREFIXES)
            if keep:
                out.append(line)
            continue
        if keep:
            out.append(line)
    return "\n".join(out)


def _run_pwsh(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)


def _tail_lines(path: Path, n: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if len(lines) > n else lines


def digest_for(row: AgentState, cfg, run_pwsh: Optional[Callable] = None,
               dispatch_dir: Optional[Path] = None) -> tuple[str, str]:
    """(digest_text, source_sentence). `digest_text` is empty exactly when nothing was found --
    the caller shows `source_sentence` in that case; nothing here is
    ever invented."""
    if row.provider == "codex":
        if not row.job_id:
            return "", NOT_FOUND_SENTENCE
        log_path = Path(dispatch_dir if dispatch_dir is not None else cfg.dispatch_dir) / f"{row.job_id}.log"
        if not log_path.exists():
            return "", NOT_FOUND_SENTENCE
        lines = _tail_lines(log_path, CODEX_LOG_TAIL_LINES)
        digest = _cap_line_length("\n".join(lines))
        if not digest.strip():
            return "", NOT_FOUND_SENTENCE
        return digest, f"the last {len(lines)} lines of {log_path.name}"

    if not row.session_id or not row.cwd:
        return "", NOT_FOUND_SENTENCE
    argv = [
        cfg.tools.pwsh, "-NoProfile", "-File", str(_necromancy_script(cfg)),
        "-Project", row.cwd, "-Session", row.session_id,
    ]
    runner = run_pwsh or _run_pwsh
    try:
        result = runner(argv)
    except (OSError, subprocess.SubprocessError):
        return "", NOT_FOUND_SENTENCE
    text = (getattr(result, "stdout", "") or "").strip()
    if getattr(result, "returncode", 1) != 0 or not text or text.startswith("NO SESSION"):
        return "", NOT_FOUND_SENTENCE
    digest = _cap(_filter_necromancy_sections(text))
    if not digest.strip():
        return "", NOT_FOUND_SENTENCE
    return digest, f"digest of Claude session {row.session_id}"


def _default_reason(row: AgentState) -> str:
    return REASON_WORDS.get(row.status.value, "it stopped")


def _session_label(row: AgentState) -> str:
    return row.tracker_id or row.session_id or "?"


def _stand_in_row(row: AgentState) -> TaskRow:
    """When the dead/parked session was not dispatched from a queue row (no `origin`), build
    the minimal `TaskRow` `guard()`/`dispatch()` need -- same project folder the old session
    used, a summary that says what this is for a human reading the dispatch log later."""
    return TaskRow(
        id=row.tracker_id or row.session_id or "handoff",
        summary=f"continue: {row.last_action or _session_label(row)}",
        project=row.project,
        path=None,
    )


def handoff_briefing(origin: Optional[TaskRow], row: AgentState, digest: str, from_provider: str,
                     to_provider: str, reason: str, cfg) -> str:
    """The new agent's first prompt: the row's own briefing when it
    was dispatched from the queue, otherwise a stand-in header naming the project and the old
    session -- then the hand-off paragraph with the digest, either way."""
    if origin is not None:
        head = briefing_mod.build(origin, cfg)
    else:
        head = (
            f'You are continuing work in project {row.project or "?"} (folder {row.cwd or "?"}); '
            f'the previous session was named "{_session_label(row)}".'
        )
    age = format_age(row.age_seconds())
    tail = (
        f"Previous agent: {from_provider}, stopped {age} ago because {reason}. "
        f"Digest of what it did:\n{digest}\n\n"
        "Continue from where the previous agent stopped. Re-orient from this project's "
        "CLAUDE.md and CHECKPOINT.md first; trust the files over the digest when they "
        "disagree. When finished, say DONE and one line of what changed."
    )
    return f"{head}\n\n{tail}"


def handoff(row: AgentState, to_provider: str, cfg, providers: dict, agent_rows,
           origin: Optional[TaskRow] = None, reason: str = "", from_provider: Optional[str] = None,
           digest_fn: Callable = digest_for) -> LaunchResult:
    """Send a dead/parked/failed row's work to `to_provider`.

    `providers` maps provider name -> its adapter, switched-on ones only (the same shape
    `providers.get_providers(cfg)` returns). The old row is left exactly as it is (the deck's
    `_fold_pantheon` ignores a `handoff` event for the OLD session id); the new agent gets its
    own row, tagged with the same tracker id so the queue's dispatched line follows it.
    """
    from_provider = from_provider or row.provider
    reason = reason or _default_reason(row)
    digest, source_sentence = digest_fn(row, cfg)
    digest_text = digest if digest else source_sentence

    task = origin if origin is not None else _stand_in_row(row)
    briefing_text = handoff_briefing(origin, row, digest_text, from_provider, to_provider, reason, cfg)

    provider = providers.get(to_provider)
    if provider is None:
        return LaunchResult(False, message=f"'{to_provider}' is not switched on in pantheon.toml")
    available = [name for name, prov in providers.items() if prov is not None]
    refusal = launch_mod.guard(task, cfg, agent_rows, to_provider, available, confirmed=True)
    if refusal is not None:
        return LaunchResult(False, message=refusal.reason)

    result = launch_mod.dispatch(task, cfg, provider, briefing_text=briefing_text)
    if result.ok:
        try:
            events_mod.append_event(cfg.events_file, Event(
                ts=utcnow_iso(), event="handoff", source="pantheon", session_id=row.session_id,
                job_id=result.job_id, cwd=row.cwd, project=row.project, tmux_pane=result.tmux_pane,
                message=result.message,
                extra={
                    "to_provider": to_provider, "from_provider": from_provider,
                    "tracker_id": task.id, "new_window_index": result.window_index,
                },
            ))
        except OSError:
            pass  # the hand-off already happened; a missing log line is not worth a second failure
        if row.status == AgentStatus.PARKED and row.session_id:
            parked_mod.append_resumed(cfg, row.session_id, by="handoff")
    return result
