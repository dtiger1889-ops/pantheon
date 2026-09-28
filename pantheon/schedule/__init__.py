"""Timed session starts.

`plan.py` is pure: when does "06:00" mean, and does that time land inside a usage window that is
already nearly spent. `schedule.py` does the real work: one Windows Task Scheduler entry per
start (`schtasks`, no always-on loop of our own) plus a small note in `state/scheduled/` so
`bin/schedule --list` can show what is waiting. `cli.py` is `bin/schedule`.
"""
