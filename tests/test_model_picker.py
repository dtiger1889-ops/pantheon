"""The model picker: family first, then version; models not used lately wait
behind a visible `show all` line; the most recently used come first; the new-session chain and the
row action `m` open the same two screens."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from pathlib import Path

from textual.widgets import OptionList, Static

from pantheon import config as config_mod
from pantheon import model_picker as mp
from pantheon.supervisor.app import SupervisorApp
from pantheon.widgets.modal import Pick

from .test_new_session_flow import (_screen_title, drives_the_screen, make_cfg, pick_project, start_deck,
                                    wait_launch)
from .test_supervisor_app import PANTHEON_WINDOW, FakeTmuxRun, make_config

NOW = 1_790_000_000.0


def cols(line: str) -> list[str]:
    """A picker line's words, with the current-value mark taken away: [key, label, ...]."""
    return line.replace("●", " ").split()


def lines(app) -> list[str]:
    options = app.screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


# --------------------------------------------------------------------------- ranking, no screen

def test_versions_used_lately_come_first_and_the_rest_wait_behind_show_all():
    usage = {"claude-opus-4-6": NOW - 3600, "claude-opus-5-5": NOW - 60}
    shown, hidden = mp.rank_versions("claude", "Opus", usage)
    assert [m.value for m in shown] == ["claude-opus-5-5", "claude-opus-4-6"]
    assert hidden == 6                                             # 5.5 1M, plan, 5, 4.8, 4.7, 4.5
    everything, none_hidden = mp.rank_versions("claude", "Opus", usage, show_all=True)
    assert none_hidden == 0
    assert {m.value for m in everything} == {m.value for m in mp.CLAUDE_MODELS if m.family == "Opus"}
    assert [m.value for m in everything][:2] == ["claude-opus-5-5", "claude-opus-4-6"]


def test_the_newest_version_and_the_current_model_are_never_hidden():
    shown, _ = mp.rank_versions("claude", "Sonnet", {})
    assert [m.value for m in shown] == ["claude-sonnet-5"]
    shown, _ = mp.rank_versions("claude", "Sonnet", {}, current="Sonnet 4.6")
    assert [m.value for m in shown] == ["claude-sonnet-5", "claude-sonnet-4-6"]


def test_families_are_ordered_by_their_most_recent_use():
    usage = {"claude-haiku-4-5-20251001": NOW - 10, "claude-opus-4-8": NOW - 100}
    assert [f.name for f in mp.rank_families("claude", usage)] == ["Haiku", "Opus", "Fable", "Sonnet"]
    assert [f.name for f in mp.rank_families("claude", {})] == ["Fable", "Opus", "Sonnet", "Haiku"]


def test_every_model_the_old_flat_list_offered_is_still_reachable():
    old = ["claude-fable-5", "claude-opus-5-5", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
           "claude-sonnet-4-6", "claude-sonnet-4-5", "opusplan", "claude-opus-4-8"]
    values = mp.all_values("claude")
    assert all(v in values for v in old)
    for alias in ("fable", "opus", "sonnet", "haiku", "opus[1m]", "sonnet[1m]"):
        assert mp.resolve(alias) in values
    assert {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6", "gpt-5.6-mini"} <= set(mp.all_values("codex"))


def test_resolve_reads_ids_short_names_and_status_line_names():
    assert mp.resolve("claude-opus-5-5") == "claude-opus-5-5"
    assert mp.resolve("Opus 5.5") == "claude-opus-5-5"
    assert mp.resolve("claude-opus-5-5", big_context=True) == "claude-opus-5-5[1m]"
    assert mp.resolve("sonnet[1m]") == "claude-sonnet-5[1m]"
    assert mp.resolve("claude-haiku-4-5") == "claude-haiku-4-5-20251001"
    assert mp.resolve("default") is None and mp.resolve("<synthetic>") is None
    assert mp.family_of("fable") == "Fable"


def test_read_usage_takes_every_local_source_and_skips_old_files(tmp_path):
    claude_home, codex_home, state = tmp_path / "claude", tmp_path / "codex", tmp_path / "state"
    proj = claude_home / "projects" / "C--x"
    proj.mkdir(parents=True)
    recent = proj / "a.jsonl"
    recent.write_text(json.dumps({"message": {"model": "claude-fable-5-1"}}) + "\n"
                      + json.dumps({"input": {"model": "opus"}}) + "\n", encoding="utf-8")   # a short name in
    os.utime(recent, (NOW - 7200, NOW - 7200))                                             # a tool call: ignored
    stale = proj / "b.jsonl"
    stale.write_text(json.dumps({"message": {"model": "claude-sonnet-4-6"}}) + "\n", encoding="utf-8")
    os.utime(stale, (NOW - 40 * 86400, NOW - 40 * 86400))
    rollout = codex_home / "sessions" / "2026" / "09" / "r.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"payload": {"model": "gpt-5.6-terra"}}) + "\n"
                       + json.dumps({"payload": {"model": "codex-auto-review"}}) + "\n", encoding="utf-8")
    os.utime(rollout, (NOW - 600, NOW - 600))
    (state / "statusline").mkdir(parents=True)
    capture = state / "statusline" / "s.json"
    capture.write_text(json.dumps({"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
                                   "context_window": {"context_window_size": 1000000}}), encoding="utf-8")
    os.utime(capture, (NOW - 60, NOW - 60))
    (state / "agents").mkdir(parents=True)
    (state / "agents" / "events.jsonl").write_text(
        json.dumps({"ts": "2026-09-22T12:00:00Z", "event": "control", "message": "/model haiku"}) + "\n"
        + json.dumps({"ts": "2026-09-22T12:00:00Z", "event": "new_session", "provider": "codex",
                      "launch_options": {"model": "gpt-6-astra"}}) + "\n", encoding="utf-8")
    os.utime(state / "agents" / "events.jsonl", (NOW, NOW))

    cache: dict = {}
    usage = mp.read_usage(claude_home, codex_home, state, now=NOW, cache=cache)
    assert usage["claude"] == {"claude-fable-5-1": NOW - 7200, "claude-opus-5-5[1m]": NOW - 60,
                               "claude-haiku-4-5-20251001": usage["claude"]["claude-haiku-4-5-20251001"]}
    assert "claude-sonnet-4-6" not in usage["claude"]                    # older than RECENT_DAYS
    assert set(usage["codex"]) == {"gpt-5.6-terra", "gpt-6-astra"}
    # A second read with the same cache answers the same without opening unchanged files.
    assert str(recent) in cache["claude"]
    assert mp.read_usage(claude_home, codex_home, state, now=NOW, cache=cache) == usage


# --------------------------------------------------------------------------- the screens

AGO = {"claude-opus-5-5": 120, "claude-opus-4-8": 3 * 3600, "claude-fable-5-1": 86400}


def _with_usage(monkeypatch, ago=AGO):
    """Usage dated from the moment the picker asks (the suite can run for minutes)."""
    monkeypatch.setattr(mp, "load_usage",
                        lambda cfg: {"claude": {k: time.time() - s for k, s in ago.items()}, "codex": {}})


def test_m_opens_families_first_then_versions_with_show_all(tmp_path, monkeypatch):
    from pantheon import tmuxctl

    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    _with_usage(monkeypatch)
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])
    seen: dict = {}

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("m"); await pilot.pause()
            seen["family_title"] = app.screen.title_text
            seen["families"] = lines(app)
            await pilot.press("o"); await pilot.pause()
            seen["version_title"] = app.screen.title_text
            seen["versions"] = lines(app)
            await pilot.press("a"); await pilot.pause()                    # show all
            seen["all"] = lines(app)
            await pilot.press("escape"); await pilot.pause()               # back to families
            seen["back"] = app.screen.title_text
            await pilot.press("o"); await pilot.pause()
            await pilot.press("2"); await pilot.pause()                    # 4.8, second most recent
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(drive())
    assert seen["family_title"].startswith("pick a model · family first")
    assert [cols(line)[1] for line in seen["families"]] == ["Opus", "Fable", "Sonnet", "Haiku", "default"]
    assert "used 2m ago" in seen["families"][0] and "more under show all" in seen["families"][0]
    assert seen["version_title"].startswith("pick a model · Opus version")
    assert [cols(line)[0] for line in seen["versions"]] == ["1", "2", "a"]
    assert "5.5" in seen["versions"][0] and "claude-opus-5-5" in seen["versions"][0]
    assert "4.8" in seen["versions"][1]
    assert cols(seen["versions"][-1])[0:2] == ["a", "show"] and "not used in 30 days" in seen["versions"][-1]
    assert not any("4.7" in line for line in seen["versions"])        # never used: hidden...
    assert any("4.7" in line for line in seen["all"])                 # ...but one key away
    assert len(seen["all"]) == len([m for m in mp.CLAUDE_MODELS if m.family == "Opus"])
    assert seen["back"].startswith("pick a model · family first")
    assert "asked window 3 to switch to claude-opus-4-8" in status
    assert ("send-keys", "-t", "pantheon:3", "-l", "/model claude-opus-4-8") in fake.calls


def test_every_family_and_version_line_can_be_clicked(tmp_path, monkeypatch):
    from pantheon import tmuxctl

    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    _with_usage(monkeypatch)
    app = SupervisorApp(cfg=make_config(tmp_path), window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("m"); await pilot.pause()
            await pilot.click(OptionList, offset=(4, 2)); await pilot.pause()     # the Fable line (row 0 is the border)
            title = app.screen.title_text
            await pilot.click(OptionList, offset=(4, 1)); await pilot.pause()     # its first version
            return title

    title = asyncio.run(drive())
    assert "Fable version" in title
    assert ("send-keys", "-t", "pantheon:3", "-l", "/model claude-fable-5-1") in fake.calls


@drives_the_screen
async def test_the_new_session_model_step_is_the_same_picker(tmp_path, monkeypatch):
    cfg, root = make_cfg(tmp_path)
    app, claude, codex, typed = await start_deck(cfg, monkeypatch)
    _with_usage(monkeypatch)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "habit")
        await pilot.press("c"); await pilot.pause()
        assert _screen_title(app).startswith("model for habit_notes · family first")
        assert cols(lines(app)[0])[1] == "Opus"                     # most recent family first
        await pilot.press("o"); await pilot.pause()
        assert _screen_title(app).startswith("model for habit_notes · Opus version")
        await pilot.press("escape"); await pilot.pause()               # version -> families, not out
        assert _screen_title(app).startswith("model for habit_notes · family first")
        await pilot.press("o"); await pilot.pause()
        await pilot.press("a"); await pilot.pause()                    # show all
        pick = [line for line in lines(app) if "claude-opus-4-7" in line][0]
        await pilot.press(cols(pick)[0]); await pilot.pause()
        assert _screen_title(app).startswith("effort for")
        await pilot.press("enter"); await pilot.pause()
        await pilot.press("enter"); await pilot.pause()
        await pilot.press("enter")
        await wait_launch(app, claude, pilot)
    assert claude.calls[0]["options"]["start_model"] == "claude-opus-4-7"


@drives_the_screen
async def test_codex_gets_its_own_families(tmp_path, monkeypatch):
    cfg, root = make_cfg(tmp_path)
    app, claude, codex, typed = await start_deck(cfg, monkeypatch)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "alma")
        await pilot.press("x"); await pilot.pause()
        assert [cols(line)[0] for line in lines(app)] == ["6", "5", "d"]
        assert "GPT-6" in lines(app)[0] and "GPT-5.6" in lines(app)[1]
        await pilot.press("5"); await pilot.pause()
        await pilot.press("a"); await pilot.pause()
        assert any("gpt-5.6-mini" in line for line in lines(app))
