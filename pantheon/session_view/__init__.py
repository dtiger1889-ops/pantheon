"""The condensed session view: a Claude session's own transcript
drawn the way Claude Desktop draws a conversation -- your messages, the assistant's prose,
tool calls as one-line chips, thinking folded away -- beside a sidebar of sessions.

`models.py` is the shared seam every package in the build imports; the parser
(`transcript.py`), the sidebar data (`recent.py`) and the widget (`conversation.py`) are
built against it in separate worktrees.
"""
