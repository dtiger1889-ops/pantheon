"""with a fake `pantheon_tmux_reachable` returning True, `delegate` builds
the ssh `new-window` command with the correct tier->model, the env-scrub prefix, `-c <dir>`, and
the task; returning False, it routes to the path-B `delegate-claude` invocation. Both driven from
a fake subprocess runner -- neither hangs, and no real ssh/tmux/claude call happens here."""
from __future__ import annotations

import subprocess

from pantheon.dispatch import delegate


def test_path_a_when_reachable_builds_ssh_new_window_with_planner_model():
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = delegate.delegate(
        "read CHECKPOINT.md and summarize open threads",
        tier="planner", directory="/c/proj", project="proj",
        reachable=lambda: True, runner=runner,
    )

    assert result.ok
    assert result.mode == "tmux"
    argv = seen["argv"]
    assert argv[0] in ("ssh", "C:/Windows/System32/OpenSSH/ssh.exe")
    assert "BatchMode=yes" in argv
    joined = " ".join(argv)
    assert "new-window" in joined
    assert "-n proj" in joined
    assert "-c /c/proj" in joined
    assert "claude-opus-5-5" in joined            # planner tier resolved
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0" in joined
    assert "unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT" in joined
    assert "read CHECKPOINT.md and summarize open threads" in joined
    assert seen["kwargs"]["timeout"] == delegate.LAUNCH_TIMEOUT_SECONDS


def test_path_a_default_tier_resolves_to_sonnet():
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    delegate.delegate("task", directory="/c/proj", reachable=lambda: True, runner=runner)
    assert "--model sonnet" in " ".join(seen["argv"])


def test_path_a_explicit_model_overrides_tier():
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    delegate.delegate("task", tier="planner", model="claude-opus-4-8-custom",
                      directory="/c/proj", reachable=lambda: True, runner=runner)
    assert "--model claude-opus-4-8-custom" in " ".join(seen["argv"])


def test_path_a_names_the_window_for_the_project_and_reports_job_and_log():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "", "")

    class FakeCfg:
        dispatch_dir = "/c/proj/state/dispatch"

    result = delegate.delegate("task", directory="/c/proj", project="my-proj",
                               cfg=FakeCfg(), reachable=lambda: True, runner=runner)
    assert result.job_id is not None
    assert "my-proj" in result.job_id
    assert result.log_path == "/c/proj/state/dispatch/" + result.job_id + ".log"
    assert "pantheon window" in result.message


def test_path_b_when_not_reachable_shells_delegate_claude():
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "hello from claude", "")

    result = delegate.delegate("summarize the open threads", reachable=lambda: False, runner=runner)

    assert result.ok
    assert result.mode == "fallback"
    assert seen["argv"][0] == "delegate-claude"
    assert "summarize the open threads" in seen["argv"]
    assert result.message == "hello from claude"


def test_path_b_receives_tier_model_perm_dir_flags():
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    delegate.delegate(
        "port the reader to v5", tier="planner", model="claude-opus-4-8", perm="bypassPermissions",
        directory="/c/proj", reachable=lambda: False, runner=runner,
    )
    argv = seen["argv"]
    assert argv[0] == "delegate-claude"
    assert "-Tier" in argv and argv[argv.index("-Tier") + 1] == "planner"
    assert "-Model" in argv and argv[argv.index("-Model") + 1] == "claude-opus-4-8"
    assert "-Perm" in argv and argv[argv.index("-Perm") + 1] == "bypassPermissions"
    assert "-Dir" in argv and argv[argv.index("-Dir") + 1] == "/c/proj"


def test_never_hangs_when_ssh_launch_raises_falls_back_to_path_b():
    calls = []

    def flaky_runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] != "delegate-claude":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 10))
        return subprocess.CompletedProcess(argv, 0, "fallback result", "")

    result = delegate.delegate("task", reachable=lambda: True, runner=flaky_runner)

    assert result.ok
    assert result.mode == "fallback"
    assert len(calls) == 2
    assert "fell back to path B" in result.message
    assert "fallback result" in result.message


def test_falls_back_when_ssh_window_open_exits_nonzero():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] != "delegate-claude":
            return subprocess.CompletedProcess(argv, 1, "", "no such session")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = delegate.delegate("task", reachable=lambda: True, runner=runner)
    assert result.ok
    assert result.mode == "fallback"
    assert len(calls) == 2


def test_empty_task_refused_without_touching_runner_or_reachable():
    def runner(argv, **kwargs):
        raise AssertionError("runner should never be called for an empty task")

    def reachable():
        raise AssertionError("reachable() should never be called for an empty task")

    result = delegate.delegate("   ", reachable=reachable, runner=runner)
    assert not result.ok
    assert result.mode == "fallback"


def test_resolve_model_unknown_tier_falls_back_to_default_tier():
    assert delegate.resolve_model("nonsense-tier", None) == delegate.TIER_LADDER[delegate.DEFAULT_TIER]


def test_build_fallback_command_omits_unset_flags():
    argv = delegate.build_fallback_command("do it")
    assert argv == ["delegate-claude", "do it"]
