"""Install scope detection behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.auto_update.platform import detect_install_scope


def test_unsupported_when_not_linux(monkeypatch):
    scope = detect_install_scope(is_linux_fn=lambda: False)
    assert scope is None


def test_user_scope_when_home_owned_by_current_user(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_system_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_user_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._user_systemd_reachable", lambda: True
    )
    scope = detect_install_scope(
        hermes_home=home,
        euid=1000,
        gateway_system_unit_exists=lambda: False,
        gateway_user_unit_exists=lambda: False,
        user_systemd_reachable=lambda: True,
        home_owner_uid=lambda p: 1000,
    )
    assert scope is not None
    assert scope.system is False
    assert scope.unit_dir == Path.home() / ".config" / "systemd" / "user"


def test_system_scope_when_gateway_system_unit_exists(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_system_unit_exists", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_user_unit_exists", lambda: False
    )
    scope = detect_install_scope(
        hermes_home=home,
        euid=1000,
        gateway_system_unit_exists=lambda: True,
        gateway_user_unit_exists=lambda: False,
        home_owner_uid=lambda p: 1000,
    )
    assert scope is not None
    assert scope.system is True
    assert scope.unit_dir == Path("/etc/systemd/system")


def test_system_scope_when_home_owned_by_different_user(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    scope = detect_install_scope(
        hermes_home=home,
        euid=1000,
        gateway_system_unit_exists=lambda: False,
        gateway_user_unit_exists=lambda: False,
        user_systemd_reachable=lambda: True,
        home_owner_uid=lambda p: 2000,
    )
    assert scope is not None
    assert scope.system is True
    assert scope.unit_dir == Path("/etc/systemd/system")


def test_user_scope_unavailable_without_user_manager(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_system_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_user_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._user_systemd_reachable", lambda: False
    )
    assert detect_install_scope(
        hermes_home=home,
        euid=1000,
        gateway_system_unit_exists=lambda: False,
        gateway_user_unit_exists=lambda: False,
        user_systemd_reachable=lambda: False,
        home_owner_uid=lambda p: 1000,
    ) is None


def _sentinel(*args, **kwargs):
    raise AssertionError("module-level probe should not run when explicit probes are injected")


def test_explicit_probes_never_invoke_module_defaults(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_system_unit_exists", _sentinel
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_user_unit_exists", _sentinel
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._user_systemd_reachable", _sentinel
    )
    monkeypatch.setattr("plugins.auto_update.platform._home_owner_uid", _sentinel)
    scope = detect_install_scope(
        hermes_home=home,
        euid=1000,
        is_linux_fn=lambda: True,
        gateway_system_unit_exists=lambda: False,
        gateway_user_unit_exists=lambda: False,
        user_systemd_reachable=lambda: True,
        home_owner_uid=lambda p: 1000,
    )
    assert scope is not None
    assert scope.system is False
    assert scope.unit_dir == Path.home() / ".config" / "systemd" / "user"


def test_default_probes_late_bind_to_monkeypatched_module_functions(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_system_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_user_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._user_systemd_reachable", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._home_owner_uid", lambda p: 1000
    )
    scope = detect_install_scope(hermes_home=home, euid=1000)
    assert scope is not None
    assert scope.system is False
    assert scope.unit_dir == Path.home() / ".config" / "systemd" / "user"
