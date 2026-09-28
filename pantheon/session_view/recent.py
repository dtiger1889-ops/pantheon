"""The sidebar's rows: every live agent THE PIT already
folds, plus a scan of recent finished transcripts across every project -- Claude and Codex both.

`live_entries` turns THE PIT's own `AgentState` rows into `SessionEntry`s, reusing
`supervisor/pane.py`'s state colour/label rules rather than reinventing them (same reasoning
`supervisor/rail.py` already followed). `recent_entries` is a separate, slower scan across
`~/.claude/projects/*/*.jsonl` and `~/.codex/sessions/**/rollout-*.jsonl`, cached for 30 seconds so
a 5-second deck tick never re-stats hundreds of files. `entries` is the one call the sidebar
widget makes: live rows first, then recent ones with the live session ids excluded.

Deviation, noted for the PR: the spec's Codex-live match asks for the
newest rollout "not older than the row's first event time"; `AgentState` carries only
`last_event_ts` (no first-seen field), so that half of the filter is dropped -- the newest rollout
whose first line's `cwd` matches wins outright. Documented rather than guessed.
"""
from __future__ import annotations

import json
import os
import re
import stat as stat_mod
import time as time_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config as config_mod
from .. import glyphs as glyphs_mod
from ..dispatch import sessions as sessions_mod
from ..models import AgentState
from ..supervisor import pane as pane_mod
from ..supervisor import state as state_mod
from .. import transcripts as transcripts_mod
from . import gitmarks as gitmarks_mod
from . import models as models_mod
from . import titles as titles_mod
from .models import SessionEntry

RECENT_CACHE_SECONDS = 30
# How many extra candidates the cached scan keeps beyond the caller's `limit`, so excluding the
# live session ids afterward still leaves enough rows to fill the list.
RECENT_BUFFER_PAD = 40

# module-level cache: (claude_home, codex_home, limit) -> (monotonic time, list[SessionEntry])
_recent_cache: dict = {}


def _home(value: Optional[Path], default_name: str) -> Path:
    return Path(value) if value is not None else Path.home() / default_name


def _normalize_cwd(cwd: Optional[str]) -> str:
    return str(cwd or "").replace("\\", "/").rstrip("/")


def _basename(cwd: Optional[str]) -> str:
    """Basename of a cwd, tolerant of Windows backslashes. MSYS Python's `PosixPath` does NOT
    split on `\\`, so `Path(r"C:\\Users\\...\\project_lanterns").name` returns the WHOLE string --
    which is why the live sidebar drew `C:\\Home\\x\\Documen…` for every RECENT row until the
    cwd was normalised first."""
    norm = _normalize_cwd(cwd)
    return norm.rsplit("/", 1)[-1] if norm else ""


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _glyph_key_for(needs_human: bool, style: Optional[str]) -> str:
    """Maps a `SessionEntry`'s already-folded colour role back to one of `glyphs.icon_table`'s
    keys -- the same table `pane.py`/`rail.py` draw from, just addressed by `style`/`needs_human`
    instead of the raw `AgentStatus` (`SessionEntry` does not carry that, by the models.py
    contract -- only the folded `style`/`needs_human`/`status_text`)."""
    if needs_human:
        return "attention"
    if style == "error":
        return "fail"
    if style == "muted":
        return "idle"
    return "working"


# ---------------------------------------------------------------- live rows


def live_entries(
    rows: list[AgentState],
    cfg: Optional[config_mod.Config] = None,
    claude_home: Optional[Path] = None,
    codex_home: Optional[Path] = None,
) -> list[SessionEntry]:
    """One `SessionEntry` per row THE PIT already folds, group `LIVE`."""
    cfg = cfg or config_mod.load()
    out: list[SessionEntry] = []
    for row in rows:
        out.append(_live_entry(row, cfg, claude_home, codex_home))
    return out


def _live_entry(row: AgentState, cfg: config_mod.Config, claude_home: Optional[Path],
                 codex_home: Optional[Path]) -> SessionEntry:
    style = pane_mod.row_style(row)
    provider = row.provider or "claude"
    transcript_path: Optional[str] = None
    title: Optional[str] = None
    marks = gitmarks_mod.GitMarks()

    if provider == "claude" and row.cwd:
        path = transcripts_mod.transcript_path(row.session_id, row.cwd, claude_home)
        if path.exists():
            transcript_path = str(path)
            title = sessions_mod.title_or_none(path)
            marks = gitmarks_mod.marks_for(path)
    elif provider == "codex":
        match = _newest_codex_rollout_for_cwd(row.cwd, codex_home)
        if match is not None:
            transcript_path = str(match)
            title = _codex_title(match, codex_home)
            marks = gitmarks_mod.marks_for(match)
    if not title:
        title = titles_mod.untitled(row.project, _age_since_iso(row.last_event_ts))

    if row.status in state_mod.ENDED:
        # Ended (gone, done, failed): it sits with that project's finished conversations, is
        # never counted as running, and a click resumes it instead of jumping to a window.
        return SessionEntry(
            session_id=row.session_id, group=models_mod.RECENT, project=row.project or "-",
            cwd=row.cwd or "", title=title, provider=provider, status_text=row.status.label,
            transcript_path=transcript_path, modified_ts=row.last_event_ts,
            last_commit_at=marks.last_commit_at, last_push_at=marks.last_push_at,
        )

    return SessionEntry(
        session_id=row.session_id,
        group=models_mod.LIVE,
        project=row.project or "-",
        cwd=row.cwd or "",
        title=title,
        provider=provider,
        status_text=row.status.label,
        style=style,
        needs_human=row.needs_human,
        window_index=row.window_index,
        tmux_session=row.tmux_session,
        transcript_path=transcript_path,
        modified_ts=None,
        last_commit_at=marks.last_commit_at,
        last_push_at=marks.last_push_at,
    )


# ---------------------------------------------------------------- codex helpers (shared by live +
# recent scans)


def _iter_rollouts(codex_home: Optional[Path]):
    root = _home(codex_home, ".codex") / "sessions"
    if not root.is_dir():
        return []
    try:
        return list(root.rglob("rollout-*.jsonl"))
    except OSError:
        return []


def _read_first_line(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            line = fh.readline()
    except OSError:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) else None


def _newest_codex_rollout_for_cwd(cwd: Optional[str], codex_home: Optional[Path]) -> Optional[Path]:
    if not cwd:
        return None
    target = _normalize_cwd(cwd)
    best_path: Optional[Path] = None
    best_ts = ""
    for path in _iter_rollouts(codex_home):
        rec = _read_first_line(path)
        if rec is None:
            continue
        payload = rec.get("payload") or {}
        if _normalize_cwd(payload.get("cwd")) != target:
            continue
        ts = str(rec.get("timestamp") or "")
        if best_path is None or ts > best_ts:
            best_path, best_ts = path, ts
    return best_path


CODEX_HEAD_LINES = 200   # the first user turn sits a handful of records in (after the injected
                         # plugin list, AGENTS.md and environment context)
_THREAD_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)
_codex_index_cache: dict = {}   # index path -> (mtime, size, {thread id: name})
_codex_title_cache: dict = {}   # rollout path -> (mtime, size, title-or-None)


def _codex_thread_names(codex_home: Optional[Path]) -> dict:
    """`{thread id: name}` from `~/.codex/session_index.jsonl` -- the name the Codex app shows for
    a thread (its auto-name or a rename), last line per id wins. Re-read only when it changes."""
    index = _home(codex_home, ".codex") / "session_index.jsonl"
    try:
        stat = index.stat()
    except OSError:
        return {}
    key = str(index)
    cached = _codex_index_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    names: dict = {}
    try:
        with open(index, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("id") and rec.get("thread_name"):
                    names[str(rec["id"])] = str(rec["thread_name"])
    except OSError:
        return {}
    _codex_index_cache[key] = (stat.st_mtime, stat.st_size, names)
    return names


def _codex_first_user_message(path: Path) -> Optional[str]:
    """The first real user message in a Codex rollout, through the same skip rules as a Claude
    title (`titles.py`: AGENTS.md, environment context, plugin lists, `[$skill](path)` links).
    Reads at most `CODEX_HEAD_LINES` records. A Codex guardian (safety review) thread, whose only
    "user" turns are the reviewed transcript, is named `Guardian review`."""
    weak: Optional[str] = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                if n >= CODEX_HEAD_LINES:
                    break
                if n == 0 and '"guardian' in line:
                    try:
                        meta = json.loads(line).get("payload") or {}
                    except (json.JSONDecodeError, AttributeError):
                        meta = {}
                    if str(meta.get("thread_source") or "").startswith("guardian"):
                        return "Guardian review"
                if '"role":"user"' not in line.replace(" ", ""):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "response_item":
                    continue
                cand = titles_mod.codex_message_candidate(rec.get("payload") or {})
                if cand is None:
                    continue
                if cand[1] >= titles_mod.STRONG:
                    return cand[0]
                weak = weak or cand[0]
    except OSError:
        pass
    return weak


def _codex_title(path: Path, codex_home: Optional[Path] = None) -> Optional[str]:
    """The Codex app's own thread name, else the first real user message; cached per file."""
    match = _THREAD_ID.search(path.name)
    if match:
        name = _codex_thread_names(codex_home).get(match.group(1))
        if name and name.strip():
            return titles_mod.clip(titles_mod.readable(name))
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _codex_title_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    title = _codex_first_user_message(path)
    _codex_title_cache[key] = (stat.st_mtime, stat.st_size, title)
    return title


def _age_since_iso(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return titles_mod.age_text((datetime.now(timezone.utc) - when).total_seconds())


def _first_text(content) -> Optional[str]:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])
    return None


# ---------------------------------------------------------------- recent scan


def _scan_claude_files(claude_home: Optional[Path]) -> list[tuple[float, Path, str]]:
    root = _home(claude_home, ".claude") / "projects"
    if not root.is_dir():
        return []
    out: list[tuple[float, Path, str]] = []
    try:
        project_dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []
    # `os.scandir` + one `stat` per `.jsonl`: the old `is_file` then `stat` walk stat'ed every
    # file twice.
    for proj_dir in project_dirs:
        try:
            with os.scandir(proj_dir) as it:
                for dent in it:
                    if not dent.name.endswith(".jsonl"):
                        continue
                    try:
                        st = dent.stat()
                    except OSError:
                        continue
                    if not stat_mod.S_ISREG(st.st_mode):
                        continue
                    out.append((st.st_mtime, proj_dir / dent.name, proj_dir.name))
        except OSError:
            continue
    return out


def _scan_codex_files(codex_home: Optional[Path]) -> list[tuple[float, Path]]:
    out = []
    for path in _iter_rollouts(codex_home):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append((mtime, path))
    return out


def _extract_cwd(path: Path) -> str:
    """Best-effort `cwd` for a Claude transcript, scanning for the first record that carries one
    (most record types do -- `transcripts.py`'s own schema notes)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("cwd"):
                    return str(rec["cwd"])
    except OSError:
        pass
    return ""


def _project_from_slug(slug: str, projects_root: Optional[str]) -> str:
    """Best-effort clean project name from a `~/.claude/projects/<slug>` directory name, used only
    when a transcript carries no `cwd` to read a basename from. The slug is the cwd path with every
    non-alphanumeric char flattened to `-` (`sessions.slug_for`), so it cannot be reversed exactly
    -- but projects sit one level under `projects_root`, so stripping the slugified root leaves the
    project's own (dash-for-underscore) name, e.g. `project-lanterns`, instead of the full
    `C--Home-x-Documents-Projects-project-lanterns` wall. Falls back to the last segment."""
    if not slug:
        return ""
    if projects_root:
        prefix = sessions_mod.slug_for(projects_root).rstrip("-") + "-"
        if slug.startswith(prefix) and len(slug) > len(prefix):
            return slug[len(prefix):]
    return slug.rsplit("-", 1)[-1] or slug


def _claude_recent_entry(path: Path, proj_name: str, mtime: float,
                         projects_root: Optional[str] = None) -> Optional[SessionEntry]:
    try:
        title = sessions_mod.title_or_none(path)
    except OSError:
        return None
    cwd = _extract_cwd(path)
    # The clean project name is the cwd's basename (same rule the live rows and the Codex recent
    # rows use), NOT the flattened `~/.claude/projects/<slug>` directory name -- that slug is the
    # `C--Home-x-Documents-Projects-project-lanterns` wall the user was seeing.
    project = _basename(cwd) or _project_from_slug(proj_name, projects_root)
    marks = gitmarks_mod.marks_for(path)
    return SessionEntry(
        session_id=path.stem,
        group=models_mod.RECENT,
        project=project or "-",
        cwd=cwd,
        title=title or titles_mod.untitled(project, titles_mod.age_text(time_mod.time() - mtime)),
        provider="claude",
        transcript_path=str(path),
        modified_ts=_iso(mtime),
        last_commit_at=marks.last_commit_at,
        last_push_at=marks.last_push_at,
    )


def _codex_recent_entry(path: Path, mtime: float,
                        codex_home: Optional[Path] = None) -> Optional[SessionEntry]:
    rec = _read_first_line(path)
    payload = (rec or {}).get("payload") or {}
    cwd = str(payload.get("cwd") or "")
    project = _basename(cwd) or "codex"
    title = (_codex_title(path, codex_home)
             or titles_mod.untitled(project, titles_mod.age_text(time_mod.time() - mtime)))
    marks = gitmarks_mod.marks_for(path)
    return SessionEntry(
        session_id=path.stem,
        group=models_mod.RECENT,
        project=project or "-",
        cwd=cwd,
        title=title,
        provider="codex",
        transcript_path=str(path),
        modified_ts=_iso(mtime),
        last_commit_at=marks.last_commit_at,
        last_push_at=marks.last_push_at,
    )


def _cache_key(claude_home: Optional[Path], codex_home: Optional[Path], limit: int,
               projects_root: Optional[str]) -> tuple:
    return (str(claude_home) if claude_home is not None else "",
            str(codex_home) if codex_home is not None else "", limit, projects_root or "")


def _scan_recent_uncached(claude_home: Optional[Path], codex_home: Optional[Path],
                          buffer_size: int, projects_root: Optional[str]) -> list[SessionEntry]:
    candidates: list[tuple] = []
    for mtime, path, proj_name in _scan_claude_files(claude_home):
        candidates.append((mtime, "claude", path, proj_name))
    for mtime, path in _scan_codex_files(codex_home):
        candidates.append((mtime, "codex", path, None))
    candidates.sort(key=lambda c: c[0], reverse=True)
    out: list[SessionEntry] = []
    for mtime, kind, path, proj_name in candidates[:buffer_size]:
        entry = (_claude_recent_entry(path, proj_name, mtime, projects_root) if kind == "claude"
                 else _codex_recent_entry(path, mtime, codex_home))
        if entry is not None:
            out.append(entry)
    return out


def _cached_recent_scan(claude_home: Optional[Path], codex_home: Optional[Path],
                        limit: int, projects_root: Optional[str]) -> list[SessionEntry]:
    key = _cache_key(claude_home, codex_home, limit, projects_root)
    now = time_mod.monotonic()
    cached = _recent_cache.get(key)
    if cached is not None and now - cached[0] < RECENT_CACHE_SECONDS:
        return cached[1]
    buffer_size = limit + RECENT_BUFFER_PAD
    scanned = _scan_recent_uncached(claude_home, codex_home, buffer_size, projects_root)
    _recent_cache[key] = (now, scanned)
    return scanned


def hidden(entry: SessionEntry, cfg: Optional[config_mod.Config]) -> bool:
    """A finished conversation from a throwaway folder (`[sessions] hide_folders`: temp folders by
    default), which the sidebar never lists and never makes a project heading for. Only hidden -- the transcript is never touched. A transcript with no
    `cwd` in it is judged by its `~/.claude/projects/<slug>` folder name instead."""
    cfg = cfg or config_mod.Config()
    settings = cfg.sessions_settings()
    # The workspace itself is never a throwaway, even if it sits inside a hidden folder.
    root = config_mod._norm_folder(cfg.projects_root)
    if entry.cwd:
        here = config_mod._norm_folder(entry.cwd)
        if root and (here == root or here.startswith(root + "/")):
            return False
        return settings.hides(entry.cwd)
    if not entry.transcript_path:
        return False
    slug = Path(entry.transcript_path).parent.name.lower()
    if root and slug.startswith(sessions_mod.slug_for(root).lower()):
        return False
    return any(slug.startswith(sessions_mod.slug_for(f).lower()) for f in settings.folders())


def recent_entries(
    cfg: Optional[config_mod.Config] = None,
    limit: int = 20,
    exclude: Optional[set] = None,
    claude_home: Optional[Path] = None,
    codex_home: Optional[Path] = None,
) -> list[SessionEntry]:
    """Recent finished transcripts across every project, newest first, group `RECENT`. `exclude`
    is normally the set of session ids already shown as `LIVE` rows."""
    exclude = exclude or set()
    projects_root = cfg.projects_root if cfg is not None else None
    candidates = _cached_recent_scan(claude_home, codex_home, limit, projects_root)
    out: list[SessionEntry] = []
    for entry in candidates:
        if entry.session_id in exclude or hidden(entry, cfg):
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def entries(
    rows: list[AgentState],
    cfg: Optional[config_mod.Config] = None,
    limit: int = 20,
    claude_home: Optional[Path] = None,
    codex_home: Optional[Path] = None,
) -> list[SessionEntry]:
    """Live rows, then recent ones (the sidebar's one call)."""
    cfg = cfg or config_mod.load()
    live = live_entries(rows, cfg, claude_home=claude_home, codex_home=codex_home)
    # An ENDED row from a throwaway folder is a finished conversation like any other: hidden. A
    # running one stays -- whatever is running is the user's business to see.
    live = [e for e in live if e.group == models_mod.LIVE or not hidden(e, cfg)]
    exclude = {r.session_id for r in rows if r.session_id}
    recent = recent_entries(cfg, limit=limit, exclude=exclude, claude_home=claude_home, codex_home=codex_home)
    return live + recent
