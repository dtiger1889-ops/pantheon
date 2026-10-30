#!/usr/bin/env bash
# Stand-in agent for the walk suite (tests/tmux_harness.py `fake_agent`). Never a real Claude
# or Codex: it only prints something prompt-like and echoes what it is typed, so the deck has a
# live pane to read and send keys to.
#   fake_agent.sh idle         a Claude-ish idle prompt; echoes each typed line as FAKE-LINE:<text>
#   fake_agent.sh permission   the Claude Code 2.1.283 Bash permission dialog (recorded fixture);
#                              echoes the one key it receives as FAKE-KEY:<key>, then goes idle
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-idle}"
if [ "$MODE" = "permission" ]; then
    cat "$HERE/approvals/bash_2.1.283.txt"
    IFS= read -r -n1 key
    printf '\nFAKE-KEY:%s\n' "$key"
    case "$key" in
        4) printf 'Interrupted - What should Claude do instead?\n' ;;
    esac
fi
printf 'fake-agent ready\n'
while true; do
    printf '> '
    IFS= read -r line || exit 0
    printf 'FAKE-LINE:%s\n' "$line"
done
