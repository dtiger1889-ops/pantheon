"""The launcher's own test lane: `bin/pantheon` driven against a throwaway tmux server.

Why: two launcher changes shipped from a Claude Desktop-app session, where tmux
cannot be exercised at all, and both broke the desk (a deck that took no arrow keys, then a blank
terminal). These tests run the real `bin/pantheon` -- on a private socket, against a throwaway
session named by `PANTHEON_SESSION` -- so the start and attach paths can never again ship unrun.

They skip themselves when tmux is not reachable, so the rest of the suite still passes from a
Desktop-app session; run them from a Claude session INSIDE the deck's tmux.

Isolation: `TMUX_TMPDIR` points at a fresh temp directory and `TMUX` is unset, so the launcher's
plain `tmux` reaches a server of its own -- the user's live deck is never touched. Every launcher
call writes its output to a file rather than a pipe: a pipe is inherited by the daemonised tmux
server, and `subprocess.run` would then wait for the server to die instead of returning.

Not libtmux's own `server` fixture: its `config_file` fixture writes a `~/.tmux.conf` carrying
`base-index 1`, which would renumber the deck's windows 0/1/2 to 1/2/3.
"""
from __future__ import annotations

import re
import subprocess
import time

import pytest

from tests.tmux_harness import (  # noqa: F401  (fixtures are used by name)
    LAUNCHER, ROOT, SESSION, TMUX, Launcher, _tmux_exe, _tmux_reachable, launcher, started,
    wait_until,
)

pytestmark = [
    pytest.mark.real_tmux,
    pytest.mark.skipif(not _tmux_reachable(), reason="no reachable tmux (Desktop-app session)"),
]


def test_no_attach_opens_deck_queue_and_budget(started):
    rows = started.windows()
    assert [(index, name) for index, name, _ in rows][:3] == [
        (0, "DECK"), (1, "QUEUE"), (2, "BUDGET"),
    ]


def test_the_throwaway_deck_writes_into_its_own_state_folder(started):
    """PANTHEON_STATE_DIR is honoured end to end, so a test run never lands in the live deck's
    log or events file."""
    wait_until(lambda: any(started.state_dir.iterdir()), "nothing was written to the test state dir")
    assert (started.state_dir / "agents").exists()


def test_the_server_it_starts_by_default_runs_without_its_log(started):
    """tmux -v logs every drawn character on its only thread and lagged every window,
    so a default start leaves it off."""
    folder = started.state_dir / "health" / SESSION / "tmux-log"
    assert not list(folder.glob("tmux-server-*.log")), "the server logged by default"
    assert not list(ROOT.glob("tmux-server-*.log")), "a server log landed in the checkout"


def test_the_server_it_starts_leaves_an_exit_record_when_it_ends(started):
    """The exit record: the launcher starts the server as tmux -D under a parent
    that appends the exit code and time to server-exits.jsonl."""
    import json
    records = started.state_dir / "health" / SESSION / "server-exits.jsonl"
    pid = started.tmux("display-message", "-p", "#{pid}").stdout.strip()
    started.tmux("kill-server")
    wait_until(lambda: records.exists() and pid in records.read_text(), "no exit record for the server")
    row = [json.loads(line) for line in records.read_text().splitlines() if line.strip()][-1]
    assert str(row["msys_pid"]) == pid and row["exit_code"] == 0 and row["signal"] is None


def _with_server_log_on(text: str) -> str:
    """The live config's text with `[health] server_log = true`, whether or not it has a [health]
    table or that key already (a second [health] table would not parse)."""
    if re.search(r"(?m)^\s*server_log\s*=", text):
        return re.sub(r"(?m)^(\s*)server_log\s*=.*$", r"\1server_log = true", text)
    if re.search(r"(?m)^\[health\]\s*$", text):
        return re.sub(r"(?m)^\[health\]\s*$", "[health]\nserver_log = true", text, count=1)
    return text + "\n[health]\nserver_log = true\n"


def test_server_log_switched_on_keeps_its_own_log_in_the_health_folder(launcher, tmp_path):
    """The server-exit log, opt-in: with `[health] server_log = true` the launcher
    starts the server with tmux's -v from the session's health folder. The setting goes in a copy
    named by PANTHEON_CONFIG, so a killed run never leaves the live pantheon.toml logging."""
    live = ROOT / "pantheon.toml"
    copy = tmp_path / "pantheon.toml"
    copy.write_text(_with_server_log_on(live.read_text(encoding="utf-8") if live.exists() else ""),
                    encoding="utf-8", newline="")
    launcher.env["PANTHEON_CONFIG"] = str(copy)
    launcher.run("--no-attach")
    folder = launcher.state_dir / "health" / SESSION / "tmux-log"
    wait_until(lambda: list(folder.glob("tmux-server-*.log")), "the server wrote no log of its own")
    assert not list(ROOT.glob("tmux-server-*.log")), "a server log landed in the checkout"


def test_health_switched_off_starts_the_server_without_its_log(launcher):
    """With `[health] enabled = false` no watcher
    runs to cap tmux's -v log, so the launcher must not turn that log on."""
    toml_path = ROOT / "pantheon.toml"
    original = toml_path.read_bytes() if toml_path.exists() else None   # byte-exact, as above
    try:
        text = (original.decode("utf-8") if original else "") + "\n[health]\nenabled = false\n"
        toml_path.write_text(text, encoding="utf-8", newline="")
        launcher.run("--no-attach")
        wait_until(lambda: len(launcher.windows()) >= 3, "the three deck windows never appeared")
        folder = launcher.state_dir / "health" / SESSION / "tmux-log"
        assert not list(folder.glob("tmux-server-*.log")), "the server logged with the watcher off"
        assert not list(ROOT.glob("tmux-server-*.log")), "a server log landed in the checkout"
    finally:
        if original is not None:
            toml_path.write_bytes(original)
        else:
            toml_path.unlink(missing_ok=True)


def test_attaching_lands_on_the_deck_window(started):
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    started.tmux("select-window", "-t", f"{SESSION}:3")
    started.attach_client()
    time.sleep(1.0)
    where = started.where_clients_are()
    assert list(where.values()) == [0], f"the client did not land on the deck: {where}"


def test_the_attached_client_sees_the_deck_drawn_and_it_takes_keys(started):
    """The 2026-09-03 breakage in one test: a deck that drew nothing, then took no arrow keys."""
    started.attach_client()
    tty, session, _ = started.clients()[0]
    wait_until(lambda: "PANTHEON" in started.capture(f"{session}:0"),
               "the deck never drew for the attached client")
    for key in ("Down", "Down", "Up", "Tab"):
        started.tmux("send-keys", "-t", f"{session}:0", key)
    time.sleep(1.0)
    assert "PANTHEON" in started.capture(f"{session}:0"), "the deck stopped drawing after arrow keys"
    assert not started.pane_dead(f"{session}:0"), "the deck pane died"
    assert tty  # the client is still there to see it


def test_a_second_client_does_not_move_the_first(started):
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    started.attach_client()
    first_tty, first_session, _ = started.clients()[0]
    # The user is looking at his agent in window 3 when the phone attaches.
    started.tmux("switch-client", "-c", first_tty, "-t", f"{first_session}:3")
    wait_until(lambda: started.where_clients_are().get(first_tty) == 3,
               "the first client never reached window 3")
    started.attach_client()
    time.sleep(1.0)
    where = started.where_clients_are()
    assert where[first_tty] == 3, f"the second client dragged the first to window {where[first_tty]}"
    assert sorted(where.values()) == [0, 3], f"the new client did not land on the deck: {where}"


def test_the_back_to_deck_key_is_bound_root_table_no_prefix(started):
    """the configured back key (F12 default) is bound in tmux's ROOT key
    table (no prefix), straight to `select-window -t :=DECK` -- the same binding shape as F1/F2/F3,
    which is what makes it work from inside a stock agent window (a root-table binding is
    intercepted by tmux before the pane ever sees the keystroke).

    `send-keys` cannot exercise this end-to-end: it writes bytes straight into a pane's pty as
    if the program running there received them, bypassing tmux's own client-side key-table
    lookup entirely -- so it can prove a key an APP handles (`Down`/`Tab`, the other test in this
    file), never a root-table tmux binding. Whether F12 itself actually reaches tmux from a real
    terminal is the acceptance's live-walk half -- `spikes/phone_client.py` is the tool for that, not this throwaway-server lane.
    """
    cp = started.tmux("list-keys", "-T", "root")
    assert cp.returncode == 0
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("bind-key -T root F12") and "select-window -t :=DECK" in ln for ln in lines), \
        f"F12 was not bound root-table to the deck window: {lines}"
    # F1 keeps its own, independent binding to the same target.
    assert any(ln.startswith("bind-key -T root F1 ") and "select-window -t :=DECK" in ln for ln in lines)


def test_the_back_to_deck_key_follows_pantheon_toml(launcher, tmp_path):
    """A different `[keys] back_to_deck` value in pantheon.toml changes which key tmux binds --
    proves the launcher actually reads the config rather than hard-coding F12."""
    toml_path = ROOT / "pantheon.toml"
    # Byte-exact read/write (`newline=""`): this is a real, tracked file -- restoring it must not introduce a CRLF/LF churn the next `git status` would see.
    original = toml_path.read_bytes() if toml_path.exists() else None
    try:
        text = (original.decode("utf-8") if original else "") + '\n[keys]\nback_to_deck = "F9"\n'
        toml_path.write_text(text, encoding="utf-8", newline="")
        launcher.run("--no-attach")
        wait_until(lambda: len(launcher.windows()) >= 3, "the three deck windows never appeared")
        cp = launcher.tmux("list-keys", "-T", "root")
        lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
        assert any(ln.startswith("bind-key -T root F9") and "select-window -t :=DECK" in ln for ln in lines), \
            f"F9 was not bound after [keys] back_to_deck = F9: {lines}"
    finally:
        if original is not None:
            toml_path.write_bytes(original)
        else:
            toml_path.unlink(missing_ok=True)


def test_window_names_are_uppercase_role_labels_not_program_paths(started):
    """`tmux list-windows` shows DECK/QUEUE/BUDGET, never a program path."""
    names = {name for _index, name, _pane in started.windows()}
    assert {"DECK", "QUEUE", "BUDGET"} <= names
    assert not any("python" in n.lower() or "\\" in n or "/" in n for n in names)


def _press_on_client(started, *keys: str) -> None:
    """Type into the attached client's own terminal (the outer server's pane), so the inner tmux
    reads the keys the way it reads a real keyboard -- root-table bindings included, which a
    `send-keys` on the inner server would bypass."""
    subprocess.run([TMUX, "-S", started.outer_socket, "send-keys", "-t", "clients:c1", *keys],
                   env=started.env, capture_output=True, timeout=30)


def _client_on_agent(started) -> str:
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    started.attach_client()
    tty, session, _ = started.clients()[0]
    started.tmux("switch-client", "-c", tty, "-t", f"{session}:3")
    wait_until(lambda: started.where_clients_are().get(tty) == 3, "the client never reached window 3")
    return tty


def _outer_screen(started) -> str:
    cp = subprocess.run([TMUX, "-S", started.outer_socket, "capture-pane", "-p", "-t", "clients:c1"],
                        env=started.env, capture_output=True, text=True, timeout=30)
    return cp.stdout


def test_f8_closes_an_agent_window_after_y(started):
    """the user 2026-10-08: a kill-window key in the bottom bar beside F1-F3. F8, then y."""
    _client_on_agent(started)
    wait_until(lambda: "F8 close window" in _outer_screen(started),
               "the status line on an agent window never showed the F8 label")
    _press_on_client(started, "F8")
    wait_until(lambda: "close agent?" in _outer_screen(started), "F8 never asked y/n")
    _press_on_client(started, "y")
    wait_until(lambda: 3 not in {i for i, _, _ in started.windows()}, "y did not close the agent window")
    assert {"DECK", "QUEUE", "BUDGET"} <= {name for _, name, _ in started.windows()}


def test_f8_then_n_keeps_the_agent_window(started):
    _client_on_agent(started)
    _press_on_client(started, "F8")
    wait_until(lambda: "close agent?" in _outer_screen(started), "F8 never asked y/n")
    _press_on_client(started, "n")
    time.sleep(1.0)
    assert 3 in {i for i, _, _ in started.windows()}, "n closed the agent window anyway"


def test_f8_on_the_deck_closes_nothing_and_shows_no_label(started):
    started.attach_client()
    tty, session, _ = started.clients()[0]
    wait_until(lambda: "PANTHEON" in started.capture(f"{session}:0"), "the deck never drew")
    assert "F8 close window" not in _outer_screen(started), "the deck window offered F8"
    before = {i for i, _, _ in started.windows()}
    _press_on_client(started, "F8")
    wait_until(lambda: "own q" in _outer_screen(started), "F8 on the deck never said why it did nothing")
    assert "(y/n)" not in _outer_screen(started), "F8 asked to close the deck"
    assert {i for i, _, _ in started.windows()} == before, "F8 closed a window on the deck"
    assert not started.pane_dead(f"{session}:0")


def test_the_deck_starts_the_tmux_feed_and_it_reads_agent_screens(started):
    """2026-10-08 lag fix: the deck's helper (pantheon/tmuxfeed.py) writes a fresh feed with every
    window and each agent window's screen, so the deck itself stops calling tmux each tick."""
    import json

    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "echo feed-probe-text; sleep 600")
    feed = started.state_dir / "tmux-feed.json"

    def fresh():
        try:
            data = json.loads(feed.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - data.get("at", 0) > 8:
            return None
        names = {w["name"] for w in data["windows"]}
        texts = " ".join(c["text"] for c in data["captures"].values())
        return data if {"DECK", "agent"} <= names and "feed-probe-text" in texts else None

    data = wait_until(fresh, "the deck never wrote a fresh tmux feed with the agent's screen", timeout=90)
    pid = int((started.state_dir / "tmux-feed.pid").read_text().strip())
    assert pid == data["pid"]
    assert not any(c["text"] and "PANTHEON" in c["text"] for c in data["captures"].values()), \
        "the deck's own window was read into the feed"


def test_restart_keeps_agent_windows_and_respawns_the_deck(started):
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    before = {index: pane for index, _, pane in started.windows()}
    started.run("--restart")
    wait_until(lambda: len(started.windows()) >= 4, "windows disappeared over --restart")
    after = {index: (name, pane) for index, name, pane in started.windows()}
    assert 3 in after and after[3][1] == before[3], "the agent window did not survive --restart"
    for index, name in ((0, "DECK"), (1, "QUEUE"), (2, "BUDGET")):
        assert after[index][0] == name
        assert after[index][1] != before[index], f"window {index} was not respawned"


def test_restart_all_removes_everything_then_starts_fresh(started):
    started.tmux("new-window", "-d", "-t", f"{SESSION}:3", "-n", "agent", "sleep 600")
    before = {index: pid for index, _, pid in started.windows()}
    started.run("--restart", "--all")
    # the restart now finishes in a detached process, so the old windows are still there
    # for a moment after the launcher returns -- wait for the fresh set, not just any three.
    wait_until(lambda: (lambda w: len(w) >= 3 and 3 not in {i for i, _, _ in w}
                        and all(x[2] != before.get(x[0]) for x in w))(started.windows()),
               "the deck never came back after --restart --all")
    after = {index: (name, pid) for index, name, pid in started.windows()}
    assert 3 not in after, "the agent window survived --restart --all"
    assert [(index, name) for index, (name, _) in sorted(after.items())] == [
        (0, "DECK"), (1, "QUEUE"), (2, "BUDGET"),
    ]
    for index in (0, 1, 2):
        assert after[index][1] != before[index], f"window {index} was not started fresh"
