"""Legacy unit migration safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.auto_update.legacy import (
    LEGACY_KNOWN_TIMER_BODY,
    LEGACY_TIMER,
    migrate_legacy_units,
)
from plugins.auto_update.platform import InstallScope


@pytest.fixture
def scope(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    return InstallScope(system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user"))


def test_known_legacy_timer_disabled_not_deleted(scope):
    path = scope.unit_dir / LEGACY_TIMER
    path.write_text(LEGACY_KNOWN_TIMER_BODY, encoding="utf-8")
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    result = migrate_legacy_units(scope, run_systemctl=fake_systemctl)
    assert path.exists()
    assert LEGACY_TIMER in result.disabled_units
    assert any("disable" in " ".join(c) for c in calls)


def test_unknown_legacy_timer_warns_and_refuses(scope):
    path = scope.unit_dir / LEGACY_TIMER
    path.write_text("[Unit]\nDescription=admin custom\n", encoding="utf-8")
    result = migrate_legacy_units(scope, run_systemctl=lambda args: (0, "", ""))
    assert path.exists()
    assert LEGACY_TIMER in result.refused
    assert result.warnings
