"""Systemd unit lifecycle and reconcile invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.auto_update.platform import InstallScope
from plugins.auto_update.systemd import (
    SERVICE_NAME,
    TIMER_NAME,
    disable_timer,
    reconcile_units,
    render_service_unit,
    render_timer_unit,
    service_unit_path,
    timer_unit_path,
    write_units_if_changed,
)


@pytest.fixture
def user_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    return InstallScope(system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user"))


@pytest.fixture
def cfg():
    return {
        "schedule_start_hour": 4,
        "schedule_end_hour": 8,
        "randomized_delay_sec": 1800,
    }


def test_service_unit_has_no_gateway_coupling(user_scope):
    body = render_service_unit(
        hermes_home="/tmp/hermes",
        exec_start=["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
        scope=user_scope,
    )
    assert "Type=oneshot" in body
    assert "PartOf=" not in body
    assert "BindsTo=" not in body


def test_timer_has_persistent_and_randomized_delay(cfg, user_scope):
    body = render_timer_unit(
        start_hour=cfg["schedule_start_hour"],
        end_hour=cfg["schedule_end_hour"],
        randomized_delay_sec=cfg["randomized_delay_sec"],
    )
    assert "Persistent=true" in body
    assert "RandomizedDelaySec=1800" in body
    assert "04,05,06,07:00:00" in body
    assert f"Unit={SERVICE_NAME}" in body


def test_reconcile_idempotent_and_atomic(user_scope, cfg, monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home", lambda: tmp_path / ".hermes"
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.legacy.duplicate_scheduler_present", lambda _scope: False
    )

    first = reconcile_units(cfg, enabled=True, run_systemctl=fake_systemctl)
    service_path = service_unit_path(user_scope)
    timer_path = timer_unit_path(user_scope)
    service_bytes = service_path.read_bytes()
    timer_bytes = timer_path.read_bytes()

    second = reconcile_units(cfg, enabled=True, run_systemctl=fake_systemctl)
    assert service_path.read_bytes() == service_bytes
    assert timer_path.read_bytes() == timer_bytes
    assert first.changed is True
    assert second.changed is False
    enable_calls = [c for c in calls if "enable" in c and TIMER_NAME in c]
    assert enable_calls
    assert not any("start" in c and SERVICE_NAME in c for c in calls)


def test_disabled_reconcile_preserves_explicit_disable(user_scope, cfg, monkeypatch):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    result = reconcile_units(cfg, enabled=False, run_systemctl=fake_systemctl)
    assert result.enabled is False
    assert any("disable" in c and TIMER_NAME in c for c in calls)
