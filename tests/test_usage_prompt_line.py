"""The usage line a Claude session reads before each prompt (`pantheon/hud/prompt_line.py`,
`hooks/usage_line.py`). Fixture hud.json / limits rows / transcripts only; no tmux, no network."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pantheon import config as config_mod
from pantheon.hud import prompt_line as pl

REPO = Path(__file__).resolve().parents[1]
NOW = 1_790_000_000.0          # a fixed "now" for the pure tests
ME = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER = "22222222-aaaa-bbbb-cccc-000000000002"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hud(five=47.0, week=16.0, age=60, five_reset_in=3 * 3600, week_reset_in=4 * 86400,
        source="account", now=NOW):
    return {"claude": {
        "five_hour_pct": five, "seven_day_pct": week,
        "five_hour_resets_at": iso(now + five_reset_in),
        "seven_day_resets_at": iso(now + week_reset_in),
        "five_hour_as_of": iso(now - age), "seven_day_as_of": iso(now - age),
        "five_hour_source": source, "seven_day_source": source, "source": source,
    }}


def samples(start_pct, end_pct, now=NOW, five_reset_in=3 * 3600, week=16.0, week_moved=0.0,
            span=3600, every=300):
    """Five-minute samples over the last `span` seconds climbing evenly from start to end."""
    steps = int(span // every)
    out = []
    for i in range(steps + 1):
        t = now - 60 - span + i * every
        pct = start_pct + (end_pct - start_pct) * i / steps
        out.append((t, float(int(pct)), now + five_reset_in, week + week_moved * i / steps,
                    now + 4 * 86400))
    return out


def day_of_samples(now=NOW, five_moved=40.0, week_moved=5.0):
    """A day's worth of samples, ending two hours ago, that fix the weekly ratio."""
    out = []
    n = 100
    for i in range(n + 1):
        t = now - 86000 + i * 600
        out.append((t, five_moved * i / n, now - 7200 + 3600, 10.0 + week_moved * i / n,
                    now + 4 * 86400))
    return out


def calls(n, start, end, sid=ME, weight=1000.0):
    step = (end - start) / max(1, n)
    return [(start + step * (i + 0.5), weight, sid) for i in range(n)]


# --------------------------------------------------------------------------- binding + calls left

def test_five_hour_binding_with_calls_left_and_run_out_before_reset():
    s = day_of_samples() + samples(81, 90)
    c = calls(30, NOW - 3660, NOW - 60)
    text = pl.compose(hud(five=90.0, five_reset_in=3 * 3600), s, c, ME, NOW)
    assert text.startswith("[usage] binding: 5-hour 90% used, about 35 model calls left at the last hour's pace")
    assert "runs out about" in text and "reset" in text
    assert "weekly 16% used, about" in text          # weekly measured through the day's ratio
    assert text.index("5-hour") < text.index("weekly")


def test_weekly_binding_when_the_week_runs_out_first():
    s = day_of_samples() + samples(10, 20, week=97.0, week_moved=1.0)
    c = calls(100, NOW - 3660, NOW - 60)
    text = pl.compose(hud(five=20.0, week=98.0, five_reset_in=4 * 3600), s, c, ME, NOW)
    assert text.startswith("[usage] binding: weekly 98% used")
    assert text.index("weekly") < text.index("5-hour")


def test_neither_runs_out_says_so():
    s = day_of_samples() + samples(40, 47)
    c = calls(70, NOW - 3660, NOW - 60)
    text = pl.compose(hud(five=47.0, five_reset_in=1800), s, c, ME, NOW)
    assert "binding" not in text
    assert "at this pace neither window runs out before it resets" in text


def test_without_the_weekly_ratio_there_is_no_binding_claim():
    s = samples(40, 47)                           # no day of history
    c = calls(70, NOW - 3660, NOW - 60)
    text = pl.compose(hud(five=47.0, five_reset_in=1800), s, c, ME, NOW)
    assert "5-hour 47% used, about" in text
    assert "weekly 16% used, resets" in text and "weekly 16% used, about" not in text
    assert "at this pace the 5-hour window lasts to its reset" in text


def test_an_unjudged_weekly_window_means_no_binding_label():
    s = samples(81, 90)                           # 5-hour runs out, weekly pace unknown
    text = pl.compose(hud(five=90.0), s, calls(30, NOW - 3660, NOW - 60), ME, NOW)
    assert "binding" not in text
    assert text.startswith("[usage] 5-hour 90% used, about 35 model calls left")
    assert "runs out about" in text


def test_too_little_movement_leaves_calls_out():
    s = day_of_samples() + samples(45, 47)        # two points: inside the bar's rounding
    text = pl.compose(hud(), s, calls(70, NOW - 3660, NOW - 60), ME, NOW)
    assert "model calls" not in text and "binding" not in text
    assert text.startswith("[usage] 5-hour 47% used, resets")


def test_too_few_calls_leaves_calls_out():
    s = day_of_samples() + samples(40, 47)        # the bar moved but few calls here (chat use)
    text = pl.compose(hud(), s, calls(3, NOW - 3660, NOW - 60), ME, NOW)
    assert "model calls" not in text


def test_a_reset_inside_the_hour_cuts_the_tail():
    s = samples(80, 95, span=1800)
    s = [(t - 1800, p, r - 18000, w, wr) for t, p, r, w, wr in s] + samples(0, 5, span=1500)
    tail = pl.five_hour_tail(s)
    assert tail[1] == 0.0 and tail[3] == 5.0


# --------------------------------------------------------------------------- stale / missing

def test_stale_reading_says_its_age_and_drops_calls():
    s = day_of_samples() + samples(40, 47, now=NOW - 1200)
    text = pl.compose(hud(age=1200), s, calls(70, NOW - 4800, NOW - 1260), ME, NOW)
    assert text.startswith("[usage] reading 20m old: 5-hour 47% used")
    assert "model calls" not in text


def test_very_old_or_missing_reading_prints_nothing():
    assert pl.compose(hud(age=4 * 3600), [], [], ME, NOW) is None
    assert pl.compose({}, [], [], ME, NOW) is None
    assert pl.compose({"claude": {"five_hour_pct": 5}}, [], [], ME, NOW) is None   # no time on it


def test_untrusted_source_is_not_shown_as_a_percent():
    assert pl.compose(hud(source="ccusage"), [], [], ME, NOW) is None


def test_reading_from_before_the_reset_is_not_passed_off_as_current():
    text = pl.compose(hud(age=900, five_reset_in=-300), [], [], ME, NOW)
    assert "5-hour reset at" in text and "no reading since" in text
    assert "5-hour 47%" not in text


# --------------------------------------------------------------------------- share

def test_share_split_by_measured_spend():
    c = calls(10, NOW - 600, NOW - 60, ME, 3000.0) + calls(10, NOW - 600, NOW - 60, OTHER, 1000.0)
    assert pl.session_share(c, ME, NOW) == (
        "this session ~75% of the last 15 min's spend (1 other session spending)")


def test_quiet_sessions_do_not_count_and_alone_says_nothing():
    c = calls(10, NOW - 600, NOW - 60, ME) + calls(10, NOW - 3000, NOW - 1200, OTHER)
    assert pl.session_share(c, ME, NOW) is None


def test_share_when_this_session_has_been_quiet():
    c = calls(10, NOW - 600, NOW - 60, OTHER)
    assert pl.session_share(c, ME, NOW) == "1 other session spent in the last 15 min, this one nothing yet"


def test_share_is_part_of_the_line():
    c = calls(10, NOW - 600, NOW - 60, ME) + calls(10, NOW - 600, NOW - 60, OTHER)
    text = pl.compose(hud(), [], c, ME, NOW)
    assert text.endswith("this session ~50% of the last 15 min's spend (1 other session spending)")


# --------------------------------------------------------------------------- transcripts

def _assistant(mid, when, content="text", model="claude-opus-5-5", out=100, read=10000):
    return json.dumps({"type": "assistant", "timestamp": iso(when), "requestId": "r" + mid,
                       "message": {"id": mid, "model": model, "content": [{"type": content}],
                                   "usage": {"input_tokens": 2, "output_tokens": out,
                                             "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": read}}})


def _write(path: Path, lines, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def test_split_records_count_once_and_synthetic_is_skipped():
    now = time.time()
    lines = [_assistant("m1", now - 100, "thinking"), _assistant("m1", now - 99, "tool_use"),
             _assistant("m2", now - 50), _assistant("m3", now - 40, model="<synthetic>"),
             json.dumps({"type": "user", "message": {"content": "the word \"assistant\" here"}})]
    got = pl._calls_in(lines)
    assert set(got) == {"m1", "m2"}
    assert got["m1"][1] == pytest.approx(2 + 100 * 5 + 10000 * 0.1)


def test_gather_reads_only_new_lines_and_rolls_subagents_up(tmp_path):
    now = time.time()
    projects = tmp_path / "projects"
    main = projects / "C--proj" / f"{ME}.jsonl"
    sub = projects / "C--proj" / ME / "subagents" / "agent-abc.jsonl"
    other = projects / "C--other" / f"{OTHER}.jsonl"
    _write(main, [_assistant("a1", now - 300), _assistant("old", now - 3 * 3600)])
    _write(sub, [_assistant("s1", now - 200)])
    _write(other, [_assistant("o1", now - 100)])
    paths = {"events": str(tmp_path / "missing.jsonl"), "projects": str(projects)}
    cache = {"v": 1}
    got = pl.gather_calls(paths, cache, now)
    by = sorted((sid, round(t)) for t, _, sid in got)
    assert [s for s, _ in by].count(ME) == 2 and [s for s, _ in by].count(OTHER) == 1
    # a half-written line is left for next time; a finished one is read from the old offset
    with open(main, "a", encoding="utf-8") as fh:
        fh.write(_assistant("a2", now - 10) + "\n" + _assistant("a3", now - 5)[:40])
    got = pl.gather_calls(paths, cache, now)
    assert sorted(int(now - t) for t, _, sid in got if sid == ME) == [10, 200, 300]
    key = next(k for k in cache["files"] if k.endswith(f"{ME}.jsonl".lower()))
    assert cache["files"][key]["offset"] < main.stat().st_size


def test_same_transcript_under_two_spellings_counts_once(tmp_path):
    now = time.time()
    projects = tmp_path / "projects"
    main = projects / "c--proj" / f"{ME}.jsonl"
    _write(main, [_assistant("a1", now - 60)])
    shouted = str(main).replace("c--proj", "C--PROJ")
    paths = {"events": str(tmp_path / "missing.jsonl"), "projects": str(projects)}
    got = pl.gather_calls(paths, {"v": 1}, now, extra=[shouted])
    assert len(got) == 1


def test_events_log_names_live_sessions(tmp_path):
    now = time.time()
    t = tmp_path / "elsewhere" / f"{ME}.jsonl"
    _write(t, [_assistant("a1", now - 60)])
    ev = tmp_path / "events.jsonl"
    _write(ev, [json.dumps({"ts": iso(now - 30), "source": "claude", "event": "Stop",
                            "transcript_path": str(t).replace("/", "\\")})])
    paths = {"events": str(ev), "projects": str(tmp_path / "projects")}
    got = pl.gather_calls(paths, {"v": 1}, now)
    assert [sid for _, _, sid in got] == [ME]


def test_read_back_to_stops_near_the_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "CHUNK", 512)
    path = tmp_path / "t.jsonl"
    now = time.time()
    _write(path, [json.dumps({"timestamp": iso(now - 7200 + i * 10), "pad": "x" * 100}) for i in range(720)])
    lines, end = pl.read_back_to(str(path), now - 600, lambda s: pl._json_ts(s, "timestamp"))
    assert end == path.stat().st_size
    assert 55 <= len(lines) <= 80           # the last ~ten minutes plus at most a chunk more


# --------------------------------------------------------------------------- switch + hook

def _cfg(tmp_path, prompt_line=True):
    return config_mod.Config(state_dir=str(tmp_path / "state"), claude_home=str(tmp_path / "home"),
                             usage={"prompt_line": prompt_line})


def _live_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "hud.json").write_text(json.dumps(hud(now=time.time())), encoding="utf-8")
    return state


def test_off_switch_toml_and_env(tmp_path, monkeypatch):
    _live_state(tmp_path)
    monkeypatch.delenv("PANTHEON_USAGE_LINE", raising=False)
    assert pl.line(_cfg(tmp_path)).startswith("[usage] 5-hour 47% used")
    assert pl.line(_cfg(tmp_path, prompt_line=False)) is None
    assert pl.line(_cfg(tmp_path, prompt_line="off")) is None
    monkeypatch.setenv("PANTHEON_USAGE_LINE", "0")
    assert pl.line(_cfg(tmp_path)) is None
    monkeypatch.setenv("PANTHEON_USAGE_LINE", "1")
    assert pl.line(_cfg(tmp_path, prompt_line=False)) is not None


def test_config_reads_prompt_line(tmp_path):
    toml = tmp_path / "pantheon.toml"
    toml.write_text("[usage]\nprompt_line = false\n", encoding="utf-8")
    assert config_mod.load(toml).usage_settings().prompt_line is False
    assert config_mod.Config().usage_settings().prompt_line is True


def test_any_error_is_silent(tmp_path, monkeypatch):
    _live_state(tmp_path)
    monkeypatch.delenv("PANTHEON_USAGE_LINE", raising=False)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(pl, "gather_calls", boom)
    assert pl.line(_cfg(tmp_path)) is None


def _run_hook(tmp_path, stdin: str, state_dir: Path):
    env = dict(os.environ, PANTHEON_STATE_DIR=str(state_dir))
    env.pop("PANTHEON_USAGE_LINE", None)
    return subprocess.run([sys.executable, str(REPO / "hooks" / "usage_line.py")], input=stdin,
                          capture_output=True, text=True, env=env, timeout=30)


def test_hook_prints_the_line_and_exits_zero(tmp_path):
    state = _live_state(tmp_path)
    r = _run_hook(tmp_path, json.dumps({"session_id": ME, "transcript_path": ""}), state)
    assert r.returncode == 0
    assert r.stdout.startswith("[usage] ") and r.stdout.count("\n") == 1
    assert r.stderr == ""


def test_hook_on_broken_inputs_prints_nothing_and_exits_zero(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "hud.json").write_text("{not json", encoding="utf-8")
    r = _run_hook(tmp_path, "garbage", state)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    r = _run_hook(tmp_path, "", tmp_path / "nowhere")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
