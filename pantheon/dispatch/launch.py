"""`dispatch`: the one call the queue pane makes when the user presses "work this".

Order of operations, and none of them is skipped: the guardrails (section 5), then the briefing
file, then the provider's `launch`, then one `dispatch` line in the event log so the queue can
show `dispatched` on the row and the supervisor can name the tracker id.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .. import events as events_mod
from ..models import AgentState, AgentStatus, Event, LaunchResult, TaskRow, utcnow_iso
from . import briefing as briefing_mod

BUSY = (AgentStatus.WORKING, AgentStatus.RUNNING, AgentStatus.WINDING_DOWN, AgentStatus.QUEUED)


@dataclass
class Refusal:
    reason: str
    needs_confirm: bool = False   # True: not a hard no -- the deck asks y/n and calls again with `confirmed=True`


def guard(row: Optional[TaskRow], cfg, agent_rows: Iterable[AgentState], provider_name: str,
          providers_available: Iterable[str], confirmed: bool = False) -> Optional[Refusal]:
    """None when dispatch may go ahead; otherwise why not, in a sentence for the footer."""
    ok, reason = briefing_mod.is_ready(row, cfg)
    if not ok:
        return Refusal(reason)
    available = set(providers_available)
    if provider_name not in available:
        allowed = ", ".join(sorted(available)) or "none"
        return Refusal(f"'{provider_name}' is not switched on in pantheon.toml (available: {allowed})")
    busy = sum(1 for r in agent_rows if r.status in BUSY)
    limit = int(getattr(cfg, "max_agents", 4) or 4)
    if busy >= limit:
        return Refusal(f"{busy} agents are already working (limit {limit} in pantheon.toml); wait for one to finish")
    if row.agent is not True and not confirmed:
        return Refusal("this task is on your own queue, not the Agent's plate. Start an agent on it anyway?",
                       needs_confirm=True)
    return None


def dispatch(row: TaskRow, cfg, provider, interactive: Optional[bool] = None,
            briefing_text: Optional[str] = None) -> LaunchResult:
    """Write the briefing, launch, record. Call `guard` first; this trusts its caller.

    `briefing_text`, when given, replaces the usual template."""
    caps = provider.capabilities() if hasattr(provider, "capabilities") else set()
    if interactive is None:
        interactive = "interactive_tmux" in caps
    folder = briefing_mod.project_dir(row, cfg)
    path = briefing_mod.write(row, cfg, text=briefing_text)
    result = provider.launch(str(folder), str(path), interactive)
    tracker = briefing_mod.tracker_id(row)
    try:
        events_mod.append_event(
            cfg.events_file,
            Event(
                ts=utcnow_iso(),
                event="dispatch" if result.ok else "dispatch_failed",
                source="pantheon",
                job_id=result.job_id,
                cwd=str(folder),
                project=row.project,
                tmux_pane=result.tmux_pane,
                message=result.message,
                extra={
                    "tracker_id": tracker,
                    "row_id": row.id,
                    "row_path": row.path,
                    "provider": getattr(provider, "name", "?"),
                    "where": result.where,
                    "window_index": result.window_index,
                    "briefing": str(path),
                },
            ),
        )
    except OSError:
        pass  # the launch already happened; a missing log line is not worth a second failure
    return result
