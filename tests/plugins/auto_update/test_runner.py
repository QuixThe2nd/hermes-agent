"""Runner behavior and stock updater argv boundary."""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli.update_lock import UpdateHolder
from plugins.auto_update.idle import IdleBlocker, IdleSnapshot
from plugins.auto_update.runner import (
    UPDATE_APPLY_ARGV,
    UPDATE_CHECK_ARGV,
    _check_output_indicates_update_available,
    build_stock_updater_argv,
    run_scheduled_update,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_build_stock_updater_argv_uses_public_subcommand(monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.resolve_hermes_bin", lambda: "/usr/local/bin/hermes"
    )
    assert build_stock_updater_argv("check") == [
        "/usr/local/bin/hermes",
        *UPDATE_CHECK_ARGV,
    ]
    assert build_stock_updater_argv("apply") == [
        "/usr/local/bin/hermes",
        *UPDATE_APPLY_ARGV,
    ]


def test_behind_substring_without_marker_is_not_available():
    text = "  This checkout is 5 commit(s) BEHIND origin/main"
    assert _check_output_indicates_update_available(text) is False


def test_update_available_marker_is_detected():
    text = "⚕ Update available: 2 commits behind origin/main."
    assert _check_output_indicates_update_available(text) is True


def test_runner_enabled_string_false_is_quiet_noop(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    outcome = run_scheduled_update(
        cfg={"enabled": "false", "idle_minutes": 8, "notify_on_success": "", "notify_on_failure": ""},
        evaluate_idle_fn=lambda *a, **k: pytest.fail("must not evaluate idle"),
        run_cmd=lambda argv: pytest.fail("must not invoke updater"),
    )
    assert outcome.reason == "disabled"


def test_runner_defers_when_not_idle(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {"enabled": True, "idle_minutes": 8, "notify_on_success": "", "notify_on_failure": ""},
    )
    calls = {"check": 0}

    def idle(*args, **kwargs):
        calls["check"] += 1
        return IdleSnapshot(idle=False, blockers=(IdleBlocker("streaming", "x"),))

    outcome = run_scheduled_update(evaluate_idle_fn=idle)
    assert outcome.code == 0
    assert outcome.reason.startswith("not_idle")
    assert calls["check"] == 1


def test_runner_rechecks_idle_before_apply_not_before_check(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {"enabled": True, "idle_minutes": 8, "notify_on_success": "", "notify_on_failure": ""},
    )
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    calls = {"n": 0}
    updater_calls: list[str] = []

    def idle(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 1:
            return IdleSnapshot(idle=True, blockers=())
        return IdleSnapshot(idle=False, blockers=(IdleBlocker("recent_activity", "x"),))

    def run_cmd(argv):
        updater_calls.append(argv[-1])
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="⚕ Update available: 1 commit behind origin/main.\n",
            stderr="",
        )

    outcome = run_scheduled_update(
        evaluate_idle_fn=idle,
        run_cmd=run_cmd,
    )
    assert outcome.reason.startswith("not_idle_before_apply")
    assert calls["n"] == 2
    assert updater_calls == ["--check"]


def test_runner_no_update_path(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {"enabled": True, "idle_minutes": 8, "notify_on_success": "", "notify_on_failure": ""},
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.read_live_update", lambda: None
    )

    def idle(*args, **kwargs):
        return IdleSnapshot(idle=True, blockers=())

    def run_cmd(argv):
        assert argv == build_stock_updater_argv("check")
        return subprocess.CompletedProcess(argv, 0, stdout="✓ Already up to date.\n", stderr="")

    outcome = run_scheduled_update(evaluate_idle_fn=idle, run_cmd=run_cmd)
    assert outcome.reason == "no_update"


def test_runner_success_and_failure_notifications(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {
            "enabled": True,
            "idle_minutes": 8,
            "notify_on_success": "updated ok",
            "notify_on_failure": "updated failed",
        },
    )
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )

    def idle(*args, **kwargs):
        return IdleSnapshot(idle=True, blockers=())

    steps = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="⚕ Update available: 1 commit behind origin/main.\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="done", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="⚕ Update available: 1 commit behind origin/main.\n", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="fail"),
        ]
    )

    ok = run_scheduled_update(evaluate_idle_fn=idle, run_cmd=lambda argv: next(steps))
    assert ok.reason == "updated"
    assert emitted == ["updated ok"]

    bad = run_scheduled_update(evaluate_idle_fn=idle, run_cmd=lambda argv: next(steps))
    assert bad.reason == "apply_failed"
    assert emitted[-1] == "updated failed"


def test_runner_live_update_defers(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {"enabled": True, "idle_minutes": 8, "notify_on_success": "", "notify_on_failure": ""},
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.read_live_update",
        lambda: UpdateHolder(pid=999, age_seconds=1.0),
    )
    outcome = run_scheduled_update(
        evaluate_idle_fn=lambda *a, **k: IdleSnapshot(idle=True, blockers=()),
        read_live_update_fn=lambda: UpdateHolder(pid=999, age_seconds=1.0),
    )
    assert outcome.reason == "update_in_progress"


def test_runner_lock_contention_defers(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {"enabled": True, "idle_minutes": 8, "notify_on_success": "", "notify_on_failure": ""},
    )
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)

    from contextlib import contextmanager

    @contextmanager
    def locked_false():
        yield False

    monkeypatch.setattr(
        "plugins.auto_update.runner.nonblocking_run_lock", locked_false
    )
    outcome = run_scheduled_update(
        evaluate_idle_fn=lambda *a, **k: IdleSnapshot(idle=True, blockers=()),
        run_cmd=lambda argv: pytest.fail("must not invoke updater"),
    )
    assert outcome.reason == "lock_contention"


def test_runner_check_timeout_is_non_fatal(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {
            "enabled": True,
            "idle_minutes": 8,
            "notify_on_success": "",
            "notify_on_failure": "timeout",
        },
    )
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )

    def run_cmd(argv):
        raise subprocess.TimeoutExpired(argv, 3600)

    outcome = run_scheduled_update(
        evaluate_idle_fn=lambda *a, **k: IdleSnapshot(idle=True, blockers=()),
        run_cmd=run_cmd,
    )
    assert outcome.code == 0
    assert outcome.reason == "check_timeout"
    assert emitted == ["timeout"]


def test_runner_apply_timeout_is_non_fatal(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {
            "enabled": True,
            "idle_minutes": 8,
            "notify_on_success": "",
            "notify_on_failure": "timeout",
        },
    )
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )
    calls = {"n": 0}

    def run_cmd(argv):
        if argv[-1] == "--check":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="⚕ Update available: 1 commit behind origin/main.\n",
                stderr="",
            )
        raise subprocess.TimeoutExpired(argv, 3600)

    outcome = run_scheduled_update(
        evaluate_idle_fn=lambda *a, **k: IdleSnapshot(idle=True, blockers=()),
        run_cmd=run_cmd,
    )
    assert outcome.code == 0
    assert outcome.reason == "apply_timeout"
    assert emitted == ["timeout"]


def test_runner_live_turn_lease_defers_without_updater(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {"enabled": True, "idle_minutes": 8, "notify_on_success": "", "notify_on_failure": ""},
    )
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    sentinel = home / "auto-update" / "sentinel.bin"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"UNCHANGED")

    def idle(*args, **kwargs):
        return IdleSnapshot(
            idle=False,
            blockers=(IdleBlocker("live_turn", "active session turn lease"),),
        )

    outcome = run_scheduled_update(
        evaluate_idle_fn=idle,
        run_cmd=lambda argv: pytest.fail("must not invoke updater"),
    )
    assert outcome.code == 0
    assert outcome.reason.startswith("not_idle")
    assert sentinel.read_bytes() == b"UNCHANGED"


def test_runner_live_delegation_defers_without_updater(home, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.load_auto_update_config",
        lambda: {"enabled": True, "idle_minutes": 8, "notify_on_success": "", "notify_on_failure": ""},
    )
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    sentinel = home / "auto-update" / "sentinel.bin"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"UNCHANGED")

    def idle(*args, **kwargs):
        return IdleSnapshot(
            idle=False,
            blockers=(IdleBlocker("live_delegation", "delegated agent running"),),
        )

    outcome = run_scheduled_update(
        evaluate_idle_fn=idle,
        run_cmd=lambda argv: pytest.fail("must not invoke updater"),
    )
    assert outcome.code == 0
    assert outcome.reason.startswith("not_idle")
    assert sentinel.read_bytes() == b"UNCHANGED"


def test_runner_stock_updater_boundary():
    import plugins.auto_update.runner as runner

    assert UPDATE_CHECK_ARGV == ("update", "--check")
    assert UPDATE_APPLY_ARGV == ("update", "--yes")
    assert callable(runner.build_stock_updater_argv)
    assert not hasattr(runner, "_cmd_update_impl")
