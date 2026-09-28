"""The model picker: family first, then version, most recently used first.

What the user asked for, and what this module is built around:

- NOT Claude Desktop's "More models" list, which buries the models actually in use.
- Pick the model FAMILY first (Fable, Opus, Sonnet, Haiku; Codex has its own), then the VERSION.
- Models not used lately (e.g. Opus 4.7, Sonnet 4.6) sit behind a visible
  `show all` line on the version screen. They are never removed: every model stays one key away.
- Most recently used first, read from the logs already on this machine.

Two halves:

1. Ranking (pure, no Textual): the catalogue below, `read_usage` (when each model was last used,
   from the Claude transcripts, the Codex session logs, the status-line captures and the deck's
   own event log), and `rank_families` / `rank_versions`.
2. The two screens (`open_picker`): a family `Pick`, then a version `Pick`. Both the new-session
   chain's model step and the row action `m` open the picker through this one function, so the
   two can never drift apart. Escape on the version screen goes back to families; Escape on the
   families screen hands `None` to the caller (the new-session chain treats that as "back one step").

"Used lately" is a judgment call: a model counts as used when a log
shows it within `RECENT_DAYS` days. A strict "ever used" rule would have kept Sonnet 4.6 on
screen, which was named as
clutter. The newest version of every family is always shown, used or not, so a new release is
never hidden; so is whatever the session is on right now.

Reading the logs is cheap on purpose: only transcripts touched in the last `RECENT_DAYS` days are
opened at all, only their first and last 64 KB are read (a model id is on every assistant line),
and what each file held is cached by size + modified time in `state/model_usage_cache.json`, so a
second open re-reads only the files that changed. The whole answer is also kept in memory for
`MEMO_SECONDS`. A model seen anywhere in a file is dated to that file's modified time, which is
close enough for an ordering.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

RECENT_DAYS = 30
MEMO_SECONDS = 60
_EDGE_BYTES = 65536


@dataclass(frozen=True)
class ModelChoice:
    """One pickable model. `value` is exactly what the deck types (`/model <value>` for Claude,
    `codex exec -m <value>` for Codex); `version` and `note` are what the user reads."""

    provider: str
    family: str
    version: str
    value: str
    note: str = ""


@dataclass(frozen=True)
class Family:
    provider: str
    name: str
    key: str


# Newest first inside each family (the first entry of a family is the one never hidden).
# Ids from Anthropic's models overview and the ids seen in this machine's
# transcripts on 2026-09-26; `[1m]` on a full id is documented at
# code.claude.com/docs/en/model-config.
CLAUDE_FAMILIES = [Family("claude", "Fable", "f"), Family("claude", "Opus", "o"),
                   Family("claude", "Sonnet", "s"), Family("claude", "Haiku", "h")]
CLAUDE_MODELS = [
    ModelChoice("claude", "Fable", "5.1", "claude-fable-5-1"),
    ModelChoice("claude", "Fable", "5", "claude-fable-5"),
    ModelChoice("claude", "Opus", "5.5", "claude-opus-5-5"),
    ModelChoice("claude", "Opus", "5.5", "claude-opus-5-5[1m]", "1M-token context"),
    ModelChoice("claude", "Opus", "plan", "opusplan", "Opus plans, Sonnet works"),
    ModelChoice("claude", "Opus", "5", "claude-opus-5"),
    ModelChoice("claude", "Opus", "4.8", "claude-opus-4-8"),
    ModelChoice("claude", "Opus", "4.7", "claude-opus-4-7"),
    ModelChoice("claude", "Opus", "4.6", "claude-opus-4-6"),
    ModelChoice("claude", "Opus", "4.5", "claude-opus-4-5"),
    ModelChoice("claude", "Sonnet", "5", "claude-sonnet-5"),
    ModelChoice("claude", "Sonnet", "5", "claude-sonnet-5[1m]", "1M-token context"),
    ModelChoice("claude", "Sonnet", "4.6", "claude-sonnet-4-6"),
    ModelChoice("claude", "Sonnet", "4.5", "claude-sonnet-4-5"),
    ModelChoice("claude", "Haiku", "4.5", "claude-haiku-4-5-20251001"),
]
# What Claude Code's short names stand for today. Used only to date a log line that named a short name.
CLAUDE_ALIASES = {
    "fable": "claude-fable-5-1", "best": "claude-fable-5-1", "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001",
    "opus[1m]": "claude-opus-5-5[1m]", "sonnet[1m]": "claude-sonnet-5[1m]",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
}

# Codex: the models on this box.
CODEX_FAMILIES = [Family("codex", "GPT-6", "6"), Family("codex", "GPT-5.6", "5")]
CODEX_MODELS = [
    ModelChoice("codex", "GPT-6", "astra", "gpt-6-astra"),
    ModelChoice("codex", "GPT-5.6", "sol", "gpt-5.6-sol"),
    ModelChoice("codex", "GPT-5.6", "terra", "gpt-5.6-terra"),
    ModelChoice("codex", "GPT-5.6", "5.6", "gpt-5.6"),
    ModelChoice("codex", "GPT-5.6", "mini", "gpt-5.6-mini", "smaller and cheaper"),
]

DEFAULT_DETAIL = {"claude": "keep the session's default model",
                  "codex": "the model in ~/.codex/config.toml"}
SHOW_ALL = "show-all"
FAMILY_PREFIX = "family:"


def catalogue(provider: str) -> tuple[list[Family], list[ModelChoice]]:
    if provider == "codex":
        return CODEX_FAMILIES, CODEX_MODELS
    return CLAUDE_FAMILIES, CLAUDE_MODELS


def all_values(provider: str) -> list[str]:
    return [m.value for m in catalogue(provider)[1]]


# --------------------------------------------------------------------------- matching

_DISPLAY = re.compile(r"(fable|opus|sonnet|haiku)\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def resolve(raw: Optional[str], provider: str = "claude", big_context: bool = False) -> Optional[str]:
    """The catalogue value a log string means, or None. Takes a full id (`claude-opus-5-5`,
    `claude-haiku-4-5-20251001`), a short name (`opus`, `sonnet[1m]`), or a status-line display
    name (`Opus 5.5`, `Opus 5.5 (1M context)`). `big_context` marks a 1M-token window the id
    itself does not show (the status line reports the window size separately)."""
    if not raw:
        return None
    text = str(raw).strip()
    values = set(all_values(provider))
    if provider == "codex":
        return text if text in values else None
    low = text.lower()
    one_m = big_context or "[1m]" in low or "1m context" in low or "1m-token" in low
    base = low.replace("[1m]", "")
    if base in CLAUDE_ALIASES:
        base = CLAUDE_ALIASES[base].replace("[1m]", "")
    if base not in values:
        match = _DISPLAY.search(low)
        if not match:
            return None
        family, version = match.group(1).capitalize(), match.group(2)
        found = next((m.value for m in CLAUDE_MODELS
                      if m.family == family and m.version == version and not m.note), None)
        if found is None:
            return None
        base = found
    if one_m and f"{base}[1m]" in values:
        return f"{base}[1m]"
    return base


def family_of(value: Optional[str], provider: str = "claude") -> Optional[str]:
    resolved = resolve(value, provider)
    for m in catalogue(provider)[1]:
        if m.value == resolved:
            return m.family
    return None


# --------------------------------------------------------------------------- reading the logs

_MODEL_BYTES = re.compile(rb'"model"\s*:\s*"([^"]{1,80})"')


def _note(usage: dict[str, float], value: Optional[str], when: float) -> None:
    if value and when > usage.get(value, 0.0):
        usage[value] = when


def _edges(path: Path, size: int) -> bytes:
    with open(path, "rb") as fh:
        head = fh.read(_EDGE_BYTES)
        if size <= 2 * _EDGE_BYTES:
            return head + fh.read()
        fh.seek(-_EDGE_BYTES, os.SEEK_END)
        return head + b"\n" + fh.read()


def _scan_files(paths: Iterable[Path], cutoff: float, cache: dict, keep: Callable[[str], bool]) -> dict[str, float]:
    """Model id -> newest modified time over `paths`, reading only the files newer than `cutoff`
    and only when their size or modified time differs from `cache` (updated in place)."""
    found: dict[str, float] = {}
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        key = str(path)
        entry = cache.get(key)
        if entry and entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime:
            ids = entry.get("ids") or []
        else:
            try:
                blob = _edges(path, st.st_size)
            except OSError:
                continue
            ids = sorted({m.decode("utf-8", "replace") for m in _MODEL_BYTES.findall(blob)})
            ids = [i for i in ids if keep(i)]
            cache[key] = {"size": st.st_size, "mtime": st.st_mtime, "ids": ids}
        for ident in ids:
            _note(found, ident, st.st_mtime)
    return found


def _read_statusline(folder: Path, cutoff: float, usage: dict[str, float]) -> None:
    for path in folder.glob("*.json") if folder.is_dir() else []:
        try:
            st = path.stat()
            if st.st_mtime < cutoff:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        model = data.get("model") or {}
        size = ((data.get("context_window") or {}).get("context_window_size") or 0)
        value = resolve(model.get("id") or model.get("display_name"), "claude",
                        big_context=isinstance(size, (int, float)) and size >= 1_000_000)
        _note(usage, value, st.st_mtime)


def _parse_ts(ts: str) -> Optional[float]:
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _read_events(path: Path, cutoff: float, usage: dict[str, dict[str, float]]) -> None:
    """The deck's own record: `launch_options.model` on a new session, `/model X` controls."""
    try:
        if not path.is_file() or path.stat().st_mtime < cutoff:
            return
        lines = path.read_bytes().splitlines()
    except OSError:
        return
    for line in lines:
        if b'"model"' not in line and b"/model " not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        when = _parse_ts(event.get("ts") or "")
        if when is None or when < cutoff:
            continue
        provider = "codex" if event.get("provider") == "codex" else "claude"
        picked = (event.get("launch_options") or {}).get("model")
        message = event.get("message") or ""
        if not picked and event.get("event") == "control" and message.startswith("/model "):
            picked = message[len("/model "):].strip()
        _note(usage[provider], resolve(picked, provider), when)


def read_usage(claude_home: str | Path, codex_home: str | Path, state_dir: str | Path,
               now: Optional[float] = None, cache: Optional[dict] = None) -> dict[str, dict[str, float]]:
    """`{"claude": {value: last_used_epoch}, "codex": {...}}` from every local source. `cache`
    (the parsed cache file, updated in place) keeps unchanged transcripts from being re-read."""
    now = time.time() if now is None else now
    cutoff = now - RECENT_DAYS * 86400
    cache = {} if cache is None else cache
    usage: dict[str, dict[str, float]] = {"claude": {}, "codex": {}}

    claude_files = Path(claude_home, "projects").glob("*/*.jsonl")
    for ident, when in _scan_files(claude_files, cutoff, cache.setdefault("claude", {}),
                                   lambda i: i.startswith("claude-")).items():
        _note(usage["claude"], resolve(ident, "claude"), when)

    codex_values = set(all_values("codex"))
    codex_files = Path(codex_home, "sessions").glob("**/*.jsonl")
    for ident, when in _scan_files(codex_files, cutoff, cache.setdefault("codex", {}),
                                   lambda i: i in codex_values).items():
        _note(usage["codex"], ident, when)

    state = Path(state_dir)
    _read_statusline(state / "statusline", cutoff, usage["claude"])
    _read_events(state / "agents" / "events.jsonl", cutoff, usage)
    return usage


_MEMO: dict[str, tuple[float, dict]] = {}


def load_usage(cfg) -> dict[str, dict[str, float]]:
    """`read_usage` for a live `Config`, with the on-disk cache and a short in-memory memo."""
    key = f"{cfg.claude_home}|{cfg.codex_home}|{cfg.state_dir}"
    hit = _MEMO.get(key)
    if hit and time.time() - hit[0] < MEMO_SECONDS:
        return hit[1]
    cache_file = Path(cfg.state_dir) / "model_usage_cache.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    usage = read_usage(cfg.claude_home, cfg.codex_home, cfg.state_dir, cache=cache)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    _MEMO[key] = (time.time(), usage)
    return usage


# --------------------------------------------------------------------------- ranking

def age_words(seconds: float) -> str:
    """`just now`, `5m ago`, `3h ago`, `2d ago`."""
    minutes = int(max(seconds, 0) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


def rank_versions(provider: str, family: str, usage: dict[str, float], current: Optional[str] = None,
                  show_all: bool = False) -> tuple[list[ModelChoice], int]:
    """(the versions to list, how many are hidden). Used ones first, newest use first; then the
    rest in catalogue order (newest release first). Hidden unless `show_all`: not used in
    `RECENT_DAYS`, not the family's newest, not the current model."""
    members = [m for m in catalogue(provider)[1] if m.family == family]
    if not members:
        return [], 0
    current_value = resolve(current, provider)
    keep = {members[0].value, current_value}
    used = sorted((m for m in members if usage.get(m.value)), key=lambda m: -usage[m.value])
    rest = [m for m in members if not usage.get(m.value)]
    ordered = used + rest
    if show_all:
        return ordered, 0
    shown = [m for m in ordered if usage.get(m.value) or m.value in keep]
    return shown, len(ordered) - len(shown)


def rank_families(provider: str, usage: dict[str, float]) -> list[Family]:
    """Families with the most recent use first; never-used families keep catalogue order after."""
    families, models = catalogue(provider)
    newest = {f.name: max((usage.get(m.value, 0.0) for m in models if m.family == f.name), default=0.0)
              for f in families}
    return sorted(families, key=lambda f: (-newest[f.name], families.index(f)))


def version_label(m: ModelChoice) -> str:
    return f"{m.version} · {m.note}" if m.note else m.version


# --------------------------------------------------------------------------- the two screens

def family_options(provider: str, usage: dict[str, float], current: Optional[str], now: float):
    from .widgets.modal import PickOption

    options = []
    for fam in rank_families(provider, usage):
        shown, hidden = rank_versions(provider, fam.name, usage, current)
        last = max((usage.get(m.value, 0.0) for m in catalogue(provider)[1] if m.family == fam.name), default=0.0)
        bits = [" · ".join(dict.fromkeys(m.version for m in shown))]
        bits.append(f"used {age_words(now - last)}" if last else f"not used in {RECENT_DAYS} days")
        if hidden:
            bits.append(f"{hidden} more under show all")
        options.append(PickOption(fam.key, FAMILY_PREFIX + fam.name, fam.name, "  ".join(bits)))
    options.append(PickOption("d", "default", "default", DEFAULT_DETAIL.get(provider, "")))
    return options


def version_options(provider: str, family: str, usage: dict[str, float], current: Optional[str],
                    now: float, show_all: bool = False):
    from .widgets.modal import PickOption

    shown, hidden = rank_versions(provider, family, usage, current, show_all)
    options = []
    for i, m in enumerate(shown[:9]):
        when = usage.get(m.value)
        detail = f"{m.value}  " + (f"used {age_words(now - when)}" if when else f"not used in {RECENT_DAYS} days")
        options.append(PickOption(str(i + 1), m.value, version_label(m), detail))
    for j, m in enumerate(shown[9:]):   # more than nine: letters, never `a` (that is show all)
        when = usage.get(m.value)
        detail = f"{m.value}  " + (f"used {age_words(now - when)}" if when else f"not used in {RECENT_DAYS} days")
        options.append(PickOption(chr(ord("b") + j), m.value, version_label(m), detail))
    if hidden:
        options.append(PickOption("a", SHOW_ALL, "show all",
                                  f"{hidden} more, not used in {RECENT_DAYS} days"))
    return options


def open_picker(app, provider: str, heading: str, current: Optional[str],
                on_done: Callable[[Optional[str]], None], cfg=None,
                usage: Optional[dict[str, float]] = None, now: Optional[float] = None) -> None:
    """Push the family screen; the version screen follows. `on_done(value)` gets the chosen
    value, `"default"`, or `None` when Escape left the family screen. `heading` is the start of
    both titles (`model for habit_notes`, `pick a model`)."""
    from .widgets.modal import Pick

    if usage is None:
        usage = load_usage(cfg).get(provider, {}) if cfg is not None else {}
    now = time.time() if now is None else now
    current_family = family_of(current, provider)
    family_current = (FAMILY_PREFIX + current_family) if current_family else (current or "default")

    def families() -> None:
        app.push_screen(
            Pick(f"{heading} · family first  (Esc = back)",
                 family_options(provider, usage, current, now), current=family_current),
            family_picked)

    def family_picked(value: Optional[str]) -> None:
        if value is None or not value.startswith(FAMILY_PREFIX):
            return on_done(value)
        versions(value[len(FAMILY_PREFIX):], False)

    def versions(family: str, show_all: bool) -> None:
        wanted = resolve(current, provider)
        app.push_screen(
            Pick(f"{heading} · {family} version  (Esc = families)",
                 version_options(provider, family, usage, current, now, show_all), current=wanted),
            lambda value: version_picked(family, value))

    def version_picked(family: str, value: Optional[str]) -> None:
        if value is None:
            return families()
        if value == SHOW_ALL:
            return versions(family, True)
        on_done(value)

    families()
