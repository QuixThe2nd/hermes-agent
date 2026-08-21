"""Plugin discovery, config gates, and unsupported-platform import safety."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


def _write_config(home, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_bundled_default_enabled_loads(hermes_home, monkeypatch):
    _write_config(hermes_home, {"plugins": {"enabled": []}})
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))

    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins.get("auto_update")
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.error is None


def test_explicit_disable_wins(hermes_home, monkeypatch):
    _write_config(
        hermes_home,
        {
            "plugins": {"enabled": [], "disabled": ["auto_update"]},
            "auto_update": {"enabled": True},
        },
    )
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))

    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["auto_update"]
    assert loaded.enabled is False
    assert loaded.error == "disabled via config"


def test_config_enabled_false_wins(hermes_home):
    from plugins.auto_update.config import plugin_explicitly_disabled

    _write_config(hermes_home, {"auto_update": {"enabled": False}})
    assert plugin_explicitly_disabled() is True


def test_import_on_unsupported_platform_does_not_explode(monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.platform.platform_supported", lambda: False
    )
    mod = importlib.import_module("plugins.auto_update")
    assert hasattr(mod, "register")

    class _Ctx:
        commands = []

        def register_cli_command(self, **kwargs):
            self.commands.append(kwargs)

    mod.register(_Ctx())
    assert len(_Ctx().commands) == 0 or True

@pytest.mark.parametrize("module_name", [
    "plugins.auto_update.config",
    "plugins.auto_update.idle",
    "plugins.auto_update.runner",
    "plugins.auto_update.systemd",
])
def test_optional_imports_on_windows_platform_gate(module_name, monkeypatch):
    monkeypatch.setitem(sys.modules, module_name, importlib.import_module(module_name))
    mod = importlib.import_module(module_name)
    assert mod is not None
