"""Dispatch: from a queue row, one key starts the right agent in that row's project with
a briefing built from the row. Nothing here writes to the vault; the only records are
`state/dispatch/*` and lines in `state/agents/events.jsonl`."""
