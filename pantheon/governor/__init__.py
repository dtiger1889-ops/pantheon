"""The session-limit governor: winds live Claude sessions
down before the user's usage limit hits, parks them safely, and brings them back once the limit
resets -- plus the trade-off hand-off to the other provider (section 11).

`policy.py` is pure decisions; `parked.py` and `handoff.py` do the file/subprocess work those
decisions need; `runner.py` is the 60-second loop that ties them together. Ships disabled and
dry (`pantheon.toml` `[governor] enabled = false, dry_run = true`) until the user turns it on.
"""
