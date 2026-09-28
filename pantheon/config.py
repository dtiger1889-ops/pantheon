"""Settings: one file, `pantheon.toml` at the project root. All paths absolute (never `~`).

Loads with the standard-library `tomllib` (the deck runs on Python 3.12 only).
Every field has a default so the deck starts even with an empty file; the first-run
wizard writes the real one.
"""
from __future__ import annotations

import os
import re
import tomllib as _toml
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOML = PROJECT_ROOT / "pantheon.toml"
STATE_DIR = PROJECT_ROOT / "state"


def _home_dir() -> str:
    """The Windows home folder with forward slashes, the base every per-user default below
    hangs off. `USERPROFILE` is the Windows home even inside an MSYS2 shell (whose own `HOME`
    is `/home/<name>`); off Windows, the ordinary home folder."""
    raw = os.environ.get("USERPROFILE") or str(Path.home())
    text = raw.replace("\\", "/").rstrip("/")
    if re.match(r"^/[a-zA-Z]/", text + "/"):
        text = f"{text[1].upper()}:{text[2:]}"
    return text


HOME = _home_dir()


@dataclass(frozen=True)
class Tools:
    """Full paths to external programs. tmux strips PATH, so nothing is called bare.

    `wt`, `codex` and `python` are machine-specific installs -- these placeholder values are
    NOT real paths. REQUIRED first-run step: set the real ones in `[tools]` in `pantheon.toml`
    (gitignored; see `pantheon.example.toml`). The wizard does not ask for these three --
    they must be hand-edited once."""

    tmux: str = "/usr/bin/tmux"  # MSYS2 tmux, seen from inside an MSYS2 shell
    npx: str = "C:/Program Files/nodejs/npx.cmd"
    node_dir: str = "C:/Program Files/nodejs"  # npx shells out to bare `node`; prepend this to PATH
    pwsh: str = "C:/Program Files/PowerShell/7/pwsh.exe"
    clip: str = "C:/Windows/System32/clip.exe"
    cmd: str = "C:/Windows/System32/cmd.exe"
    wt: str = f"{HOME}/AppData/Local/Microsoft/WindowsApps/wt.exe"  # per-user Store install; override in pantheon.toml
    codex: str = f"{HOME}/.local/bin/codex"  # the Codex CLI shim; override in pantheon.toml
    claude: str = "claude"  # typed into a bash window; the ~/.bashrc wrapper adds --permission-mode auto
    python: str = (PROJECT_ROOT / ".venv" / "bin" / "python").as_posix()  # MSYS2 venv, runs the Codex job wrapper
    ccusage: str = ""  # global install, e.g. "<home>/AppData/Roaming/npm/ccusage.cmd"; "" = via npx


@dataclass(frozen=True)
class ToolsPage:
    """tools page: the on-disk sources the read-only
    inventory reads. Named `ToolsPage`/`[tools_page]`, not `Tools`/`[tools]` -- that name and
    TOML table already belong to the external-programs settings above; this is a second,
    unrelated set of paths and needed its own name rather than colliding with them."""

    claude_skills_dir: str = f"{HOME}/.claude/skills"
    agents_skills_dir: str = f"{HOME}/.agents/skills"
    claude_settings: str = f"{HOME}/.claude/settings.json"
    codex_config: str = f"{HOME}/.codex/config.toml"
    codex_hooks: str = f"{HOME}/.codex/hooks.json"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolsPage":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**kwargs)


@dataclass(frozen=True)
class Governor:
    """session-limit governor settings;
    `Config.governor_settings` merges `[governor]` in pantheon.toml over these defaults.
    Ships disabled and dry -- flip both on after reading a real night of logged
    decisions (`state/limits/governor-dryrun.jsonl`)."""

    enabled: bool = False
    dry_run: bool = True
    grace_seconds: int = 300
    auto_resume: bool = True
    max_parked: int = 6
    resume_prompt: str = "Limit reset. Re-orient from CHECKPOINT.md, then continue where you stopped."
    wind_down_at_percent: dict[str, float] = field(
        default_factory=lambda: {"five_hour": 85.0, "seven_day": 90.0}
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Governor":
        data = dict(data or {})
        wind_down = {"five_hour": 85.0, "seven_day": 90.0}
        wind_down.update(data.pop("wind_down_at_percent", {}) or {})
        known = {f for f in cls.__dataclass_fields__ if f != "wind_down_at_percent"}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(wind_down_at_percent=wind_down, **kwargs)


@dataclass(frozen=True)
class Notify:
    """notification settings; `Config.notify_settings`
    merges `[notify]` in pantheon.toml over these defaults. Ships disabled and dry --
    turn it on after reading a day of `state/notify/sent.jsonl` in dry run."""

    enabled: bool = False
    dry_run: bool = True
    needs_you_after_seconds: int = 60
    nudge_minutes: int = 15
    max_nudges: int = 2
    present_seconds: int = 120
    quiet_hours: list[str] = field(default_factory=lambda: ["23:00", "08:00"])
    toast_enabled: bool = True
    ntfy_enabled: bool = False
    ntfy_url: str = ""
    ntfy_topic: str = "pantheon"
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    # The Windows Credential Manager target name holding the bot token -- the same one the
    # `telegram-notifier` MCP extension's `bot_token` user_config resolves to (never the token
    # itself, never written to this file or any other). Empty = telegram() refuses to send and
    # says why, rather than guessing a name (this build could not confirm the real target name
    # from Claude Desktop's own storage -- see channels.py's docstring).
    telegram_credential_target: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notify":
        data = dict(data or {})
        toast = dict(data.pop("toast", {}) or {})
        ntfy = dict(data.pop("ntfy", {}) or {})
        telegram = dict(data.pop("telegram", {}) or {})
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}
        if "enabled" in toast:
            kwargs["toast_enabled"] = toast["enabled"]
        if "enabled" in ntfy:
            kwargs["ntfy_enabled"] = ntfy["enabled"]
        if "url" in ntfy:
            kwargs["ntfy_url"] = ntfy["url"]
        if "topic" in ntfy:
            kwargs["ntfy_topic"] = ntfy["topic"]
        if "enabled" in telegram:
            kwargs["telegram_enabled"] = telegram["enabled"]
        if "chat_id" in telegram:
            kwargs["telegram_chat_id"] = telegram["chat_id"]
        if "credential_target" in telegram:
            kwargs["telegram_credential_target"] = telegram["credential_target"]
        return cls(**kwargs)


@dataclass(frozen=True)
class Appearance:
    """`[appearance]` in pantheon.toml. `Config.appearance`
    keeps the raw TOML dict (same pattern as `Governor`/`Config.governor`);
    `Config.appearance_settings` returns this typed, defaulted form."""

    theme: str = "pantheon"     # "pantheon" | "high-contrast" | "light"; also Ctrl+P -> "theme"
    glyphs: str = "unicode"     # "unicode" | "ascii" 
    clock: str = "24h"          # "24h" | "12h" -- every wall-clock time on screen (models.format_clock);
                                 #
    gauge: str = "solid"        # "solid" | "blocks": "blocks" leaves a hairline between the filled
                                # cells of a budget gauge
    hide: list[str] = field(default_factory=list)   # budget-card foot fields to drop: "today",
        # "burn", "resets", "resets_in", "runs_out", "memory", "as_of", "trend" (sparkline row),
        # "week" (weekly gauge) -
    icons: bool = False   # Nerd Font glyphs (font installed and
        # verified on this box) in THE PIT's state column and the new-session project picker's
        # tree, in place of the plain-Unicode/ASCII glyphs.table pair (pantheon/glyphs.py);
        # false = every screen renders exactly as it does today.
    # the last font the user applied through the Settings screen, remembered here so
    # `pantheon --settings` can show it even though the actual source of truth on the desk is
    # Windows Terminal's own settings.json -- `pantheon/appearance/wt.read_font` reconciles
    # against that file on open and flags a mismatch rather than trusting these blindly. "" / 0.0
    # means "never applied from here yet".
    font_face: str = ""
    font_size: float = 0.0
    cell_height: float = 0.0
    cell_width: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Appearance":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**kwargs)


@dataclass(frozen=True)
class Keys:
    """`[keys]` in pantheon.toml: the one prefix-less key
    that jumps back to the deck (window 0) from inside any agent window. F12 by default, so a
    terminal that does not transmit F12 cleanly only needs this one value
    changed, never a rebuild. `bin/pantheon` reads it at session-start to write the tmux binding;
    a change here needs a restart (`pantheon --restart`) to take effect, same as any other
    session-scoped tmux config."""

    back_to_deck: str = "F12"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Keys":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**kwargs)


@dataclass(frozen=True)
class ThemeOverrides:
    """`[theme]` in pantheon.toml: which shipped
    palette preset is active, plus any per-token overrides on top of it. Kept apart from
    `Appearance.theme` -- `preset` here is the PALETTE the Settings screen manages; `pantheon/appearance/palette.py`
    owns turning `(preset, overrides)` into an actual set of token colours, validated against
    `pantheon.theme.TOKENS`."""

    preset: str = "pantheon"   # "pantheon" | "high-contrast" | "light"
    overrides: dict[str, str] = field(default_factory=dict)  # token name -> hex colour

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThemeOverrides":
        data = dict(data or {})
        overrides = dict(data.pop("overrides", {}) or {})
        known = {f for f in cls.__dataclass_fields__ if f != "overrides"}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(overrides=overrides, **kwargs)


@dataclass(frozen=True)
class NewSession:
    """`[new_session]` in pantheon.toml: what the `n` picker
    preselects for model/effort on a fresh session, so it stops defaulting to whatever the
    installed Claude Code CLI ships with (Sonnet, low) -- a phone session came up on exactly that.
    `"default"` for either field means "don't touch it, keep the CLI's own default"; picking a real
    value here means the launcher applies it right after the session opens (`pane.py`
    `_apply_launch_options`, the same path the row toolbar's `m`/`e` actions use)."""

    model: str = "default"
    effort: str = "high"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewSession":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**kwargs)


@dataclass(frozen=True)
class Standalone:
    """`[standalone]` in pantheon.toml: the plain
    folder of Markdown files anyone without Obsidian can point Pantheon at instead of the vault
. `Config.standalone_settings` merges the raw dict over these defaults, same pattern
    as `Governor`/`Appearance`."""

    folder: str = f"{HOME}/Documents/pantheon-tasks"  # REQUIRED if task_source = "standalone"; set for real in pantheon.toml

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Standalone":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**kwargs)


@dataclass(frozen=True)
class Usage:
    """`[usage]` in pantheon.toml. `account_read` lets the usage collector read the account's own
    usage numbers from Anthropic's usage endpoint as a second, less-trusted source behind the
    status line. `false` = the status line and local logs only, as before.

    `prompt_line` turns the one usage line a Claude session reads before each prompt on or off
    (`hooks/usage_line.py`, `pantheon/hud/prompt_line.py`; the env var `PANTHEON_USAGE_LINE`
    beats it). It only matters once that hook is registered in Claude Code's settings."""

    account_read: bool = True
    prompt_line: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Usage":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        for key in ("account_read", "prompt_line"):
            if key in kwargs:
                value = kwargs[key]
                kwargs[key] = not (
                    value is False or str(value).strip().lower() in ("false", "off", "no", "0"))
        return cls(**kwargs)


@dataclass(frozen=True)
class Assistant:
    """`[assistant]` in pantheon.toml: the one pinned Claude Code
    session that stands in for Claude Desktop's Dispatch.
    `enabled = false` turns the whole thing off (no window, no sidebar line); `folder` is where it
    works, "" = the workspace root (`Config.projects_root`). `model` and `effort` are passed on
    the command line every time it starts or is resumed; "" = Claude Code's own default. `name` is the
    display name a NEW conversation gets at its first start (`--name`); a resumed or adopted one
    keeps whatever name it has. `memory` is the long-term memory folder the Assistant shares with
    Claude Desktop's Dispatch; "" = find
    Dispatch's own `agent/memory` folder, "off" = no shared memory."""

    enabled: bool = True
    folder: str = ""
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    name: str = "Pantheon Assistant"
    memory: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Assistant":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        if "enabled" in kwargs:
            value = kwargs["enabled"]
            kwargs["enabled"] = not (
                value is False or str(value).strip().lower() in ("false", "off", "no", "0"))
        for key in ("folder", "model", "effort", "name", "memory"):
            if key in kwargs:
                kwargs[key] = str(kwargs[key] or "").strip()
        return cls(**kwargs)


# Where throwaway trials run (a test tmux server, a probe folder): their transcripts are real
# Claude conversations, but they are not the user's projects.
DEFAULT_HIDE_FOLDERS = ("C:/msys64/tmp", "/tmp", "%LOCALAPPDATA%/Temp", "C:/Windows/Temp")


def _norm_folder(path: str) -> str:
    """One spelling for a folder so two spellings of it compare equal: forward slashes, lower
    case, no trailing slash, and MSYS2's `/c/...` as `c:/...`."""
    text = str(path or "").strip().replace("\\", "/").rstrip("/").lower()
    if re.match(r"^/[a-z]/", text + "/"):
        text = f"{text[1]}:{text[2:]}"
    return text


@dataclass(frozen=True)
class Sessions:
    """`[sessions]` in pantheon.toml. `hide_folders`: sessions whose folder is one of these, or
    inside one, never appear in the sidebar's finished list or make a project heading of their
    own. `%NAME%` expands from the environment (a name that is not set drops that entry). Their
    transcripts are left alone -- this only hides them."""

    hide_folders: tuple = DEFAULT_HIDE_FOLDERS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Sessions":
        raw = (data or {}).get("hide_folders", None)
        if raw is None:
            return cls()
        if isinstance(raw, str):
            raw = [raw]
        return cls(hide_folders=tuple(str(x) for x in raw if str(x).strip()))

    def folders(self) -> tuple:
        """`hide_folders` with `%NAME%` expanded and normalised; unexpandable ones dropped."""
        out = []
        for folder in self.hide_folders:
            names = re.findall(r"%([^%]+)%", folder)
            if any(not os.environ.get(n) for n in names):
                continue
            for n in names:
                folder = folder.replace(f"%{n}%", os.environ[n])
            norm = _norm_folder(folder)
            if norm:
                out.append(norm)
        return tuple(out)

    def hides(self, cwd: str) -> bool:
        """True when `cwd` is one of the hidden folders or inside one."""
        here = _norm_folder(cwd)
        if not here:
            return False
        return any(here == f or here.startswith(f + "/") for f in self.folders())


@dataclass(frozen=True)
class Config:
    """None of the fields below are real on a fresh checkout -- `vault` and `projects_root` are
    placeholders the first-run wizard asks about and fills in for real;
    `claude_home` and `codex_home` are placeholders too and need a hand-edit in `pantheon.toml`
    (gitignored; see `pantheon.example.toml`) if they are not at the Windows default. On this
    machine the real values live only in `pantheon.toml`, never here."""

    vault: str = f"{HOME}/Documents/Obsidian Vault"  # REQUIRED: the first-run wizard asks for this
    projects_root: str = f"{HOME}/Documents/Projects"  # REQUIRED: the first-run wizard asks for this
    claude_home: str = f"{HOME}/.claude"  # REQUIRED: set for real in pantheon.toml
    codex_home: str = f"{HOME}/.codex"  # REQUIRED: set for real in pantheon.toml
    tmux_session: str = "pantheon"
    refresh_seconds: int = 5  # how often a pane's timer re-reads; the queue pane treats 0 as
                              # manual only (no timer -- the `r` key still re-reads by hand)
    ccusage_version: str = "latest"  # pin once it works, e.g. "17.2.0"
    default_provider: str = "claude"
    max_agents: int = 4  # concurrency guard
    task_source: str = "obsidian_base"  # "obsidian_base" | "standalone"
    base_file: str = "Projects/Sprints.base"  # relative to `vault`; any Base with these fields works
    sprints_folder: Optional[str] = None  # relative to `vault`; None = derive from the Base's own
                                           # first `file.inFolder(...)` filter
    pinned_projects: list[str] = field(default_factory=list)  # absolute folders the `n` picker always
                                                               # lists (a nested repo like Apps/<app> the
                                                               # root scan misses and the log ages out of)
    providers: dict[str, bool] = field(
        default_factory=lambda: {"claude": True, "codex": True}
    )
    governor: dict[str, Any] = field(default_factory=dict)  # owns the keys
    notify: dict[str, Any] = field(default_factory=dict)  # owns the keys
    appearance: dict[str, Any] = field(default_factory=dict)  # owns the keys
    theme: dict[str, Any] = field(default_factory=dict)  # owns the keys ([theme] palette preset + overrides)
    standalone: dict[str, Any] = field(default_factory=dict)  # owns the keys
    new_session: dict[str, Any] = field(default_factory=dict)  # owns the keys; `[new_session]`
    keys: dict[str, Any] = field(default_factory=dict)  # owns the keys; `[keys]`
    tools: Tools = field(default_factory=Tools)
    tools_page: dict[str, Any] = field(default_factory=dict)  # owns the keys; `[tools_page]`
    editor: dict[str, Any] = field(default_factory=dict)  # owns the keys; `[editor]` (read only
                                                           # through pantheon/editor.py)
    presets: dict[str, Any] = field(default_factory=dict)  # owns the keys; `[presets.<name>]`
                                                            # tables, read only through dispatch/presets.py
    usage: dict[str, Any] = field(default_factory=dict)  # `[usage]`; read only through usage_settings()
    assistant: dict[str, Any] = field(default_factory=dict)  # `[assistant]`; read only through
                                                              # assistant_settings
    sessions: dict[str, Any] = field(default_factory=dict)  # `[sessions]`; read only through
                                                             # sessions_settings()
    state_dir: str = str(STATE_DIR)

    def __post_init__(self) -> None:
        if self.refresh_seconds < 0:
            raise ValueError(
                f"refresh_seconds must be 0 or more (0 means manual only), got {self.refresh_seconds}"
            )

    # ---- derived paths -------------------------------------------------
    @property
    def sprints_dir(self) -> Path:
        """A best-effort Sprints folder for callers that need a path before any Base has been
        read (the pre-flight check in `queue/app.py`, `pane.py`'s shell-open fallback). The
        authoritative folder -- derived from the actual Base file when `sprints_folder` is unset
        -- is `ObsidianBaseSource.folder`; this property only ever falls back to the
        historical default, never reads a file."""
        return Path(self.vault) / (self.sprints_folder or "Projects/Sprints")

    @property
    def sprints_base(self) -> Path:
        return Path(self.vault) / self.base_file

    @property
    def events_file(self) -> Path:
        return Path(self.state_dir) / "agents" / "events.jsonl"

    @property
    def hud_file(self) -> Path:
        return Path(self.state_dir) / "hud.json"

    @property
    def statusline_dir(self) -> Path:
        return Path(self.state_dir) / "statusline"

    @property
    def dispatch_dir(self) -> Path:
        return Path(self.state_dir) / "dispatch"

    @property
    def log_file(self) -> Path:
        return Path(self.state_dir) / "pantheon.log"

    @property
    def notify_dir(self) -> Path:
        return Path(self.state_dir) / "notify"

    @property
    def notify_sent_file(self) -> Path:
        return self.notify_dir / "sent.jsonl"

    @property
    def notify_last_file(self) -> Path:
        return self.notify_dir / "last.json"

    @property
    def presence_file(self) -> Path:
        """Touched by the deck on every key/mouse event. Its own file,
        not under `notify/`, so a future non-notify reader can stat it
        without importing anything notify-specific."""
        return Path(self.state_dir) / "presence"

    def enabled_providers(self) -> list[str]:
        return [name for name, on in self.providers.items() if on]

    def governor_settings(self) -> Governor:
        """`self.governor` (the raw TOML dict, kept as-is for backward compatibility and
        round-tripping) as a typed `Governor` with every default filled in."""
        return Governor.from_dict(self.governor)

    def notify_settings(self) -> Notify:
        """`self.notify` (the raw TOML dict) as a typed `Notify` with every default filled
        in -- same pattern as `governor_settings`."""
        return Notify.from_dict(self.notify)

    def appearance_settings(self) -> Appearance:
        """`self.appearance` (the raw TOML dict) as a typed `Appearance` with every default
        filled in -- same pattern as `governor_settings`."""
        return Appearance.from_dict(self.appearance)

    def theme_settings(self) -> ThemeOverrides:
        """`self.theme` (the raw `[theme]` TOML dict) as a typed `ThemeOverrides` with 
        defaults filled in -- same pattern as `governor_settings`."""
        return ThemeOverrides.from_dict(self.theme)

    def standalone_settings(self) -> Standalone:
        """`self.standalone` as a typed `Standalone` with defaults filled in."""
        return Standalone.from_dict(self.standalone)

    def new_session_settings(self) -> NewSession:
        """`self.new_session` (the raw `[new_session]` TOML dict) as a typed `NewSession` with
        its defaults filled in -- same pattern as `governor_settings()`."""
        return NewSession.from_dict(self.new_session)

    def keys_settings(self) -> Keys:
        """`self.keys` (the raw `[keys]` TOML dict) as a typed `Keys` with default filled
        in -- same pattern as `governor_settings`."""
        return Keys.from_dict(self.keys)

    def tools_page_settings(self) -> ToolsPage:
        """`self.tools_page` (the raw `[tools_page]` TOML dict) as a typed `ToolsPage` with
        defaults filled in -- same pattern as `governor_settings`."""
        return ToolsPage.from_dict(self.tools_page)

    def usage_settings(self) -> Usage:
        """`self.usage` (the raw `[usage]` TOML dict) as a typed `Usage` with its default filled
        in -- same pattern as `governor_settings()`."""
        return Usage.from_dict(self.usage)

    def assistant_settings(self) -> Assistant:
        """`self.assistant` (the raw `[assistant]` TOML dict) as a typed `Assistant`, with the
        empty folder resolved to the workspace root."""
        settings = Assistant.from_dict(self.assistant)
        if not settings.folder:
            settings = dataclasses.replace(settings, folder=self.projects_root)
        return settings

    def sessions_settings(self) -> Sessions:
        """`self.sessions` (the raw `[sessions]` TOML dict) as a typed `Sessions`."""
        return Sessions.from_dict(self.sessions)

    def node_env(self) -> dict[str, str]:
        """Environment for anything that runs npx/ccusage (node dir first on PATH)."""
        env = dict(os.environ)
        env["PATH"] = self.tools.node_dir + os.pathsep + env.get("PATH", "")
        return env


def load(path: str | os.PathLike | None = None) -> Config:
    """Read `pantheon.toml`. Missing file -> defaults (the caller decides whether to run the wizard).

    `PANTHEON_STATE_DIR` in the environment wins over the file's `state_dir`: the launcher test
    lane (tests/test_launcher_tmux.py) starts a whole throwaway deck, and without this its apps
    would write their log and their events into the live deck's `state/` folder.
    """
    toml_path = Path(path) if path else DEFAULT_TOML
    if not toml_path.exists():
        return _with_env_overrides(Config())
    with open(toml_path, "rb") as fh:
        data = _toml.load(fh)
    tools_data = data.pop("tools", {}) or {}
    providers = dict(Config().providers)
    providers.update(data.pop("providers", {}) or {})
    known = {f for f in Config.__dataclass_fields__ if f not in {"tools", "providers"}}
    kwargs = {k: v for k, v in data.items() if k in known}
    return _with_env_overrides(Config(
        tools=Tools(**{k: v for k, v in tools_data.items() if k in Tools.__dataclass_fields__}),
        providers=providers,
        **kwargs,
    ))


def _with_env_overrides(cfg: Config) -> Config:
    import dataclasses

    state_dir = os.environ.get("PANTHEON_STATE_DIR", "").strip()
    if state_dir:
        cfg = dataclasses.replace(cfg, state_dir=state_dir)
    return cfg


def ensure_state_dirs(cfg: Config) -> None:
    for p in (cfg.events_file.parent, cfg.statusline_dir, cfg.dispatch_dir,
              Path(cfg.state_dir) / "limits", cfg.notify_dir):
        p.mkdir(parents=True, exist_ok=True)
