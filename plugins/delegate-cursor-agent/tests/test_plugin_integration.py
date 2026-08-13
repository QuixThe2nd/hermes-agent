"""PluginManager / Plugin Doctor integration tests for delegate-cursor-agent."""

from __future__ import annotations

import builtins
import io
import json
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
TOOL_NAME = "delegate_cursor_agent"
PRODUCTION_HERMES_HOME = (Path.home() / ".hermes").resolve()
PRODUCTION_RUNS_DIR = PRODUCTION_HERMES_HOME / "cursor-runs"


class _MinimalStreamingPopen:
    instances: list["_MinimalStreamingPopen"] = []

    def __init__(self, cmd, **kwargs):
        self.__class__.instances.append(self)
        self.cmd = cmd
        self.cwd = kwargs.get("cwd")
        self.pid = 9000 + len(self.__class__.instances)
        self._returncode = None
        line = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "hermetic home regression ok",
            }
        )
        payload = (line + "\n").encode("utf-8")
        self._stdout_len = len(payload)
        self.stdout = io.BytesIO(payload)

    def poll(self):
        if self._returncode is not None:
            return self._returncode
        if self.stdout.closed or self.stdout.tell() >= self._stdout_len:
            self._returncode = 0
        return self._returncode

    def wait(self, timeout=None):
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9


def _load_plugin_module(*, module_name: str = "hermes_plugins.delegate_cursor_agent"):
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.delegate_cursor_agent"
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_manifest_parsing():
    from hermes_cli.plugins import PluginManager

    mgr = PluginManager()
    manifest = mgr._parse_manifest(
        PLUGIN_DIR / "plugin.yaml",
        PLUGIN_DIR,
        source="bundled",
        prefix="",
    )
    assert manifest is not None
    assert manifest.name == "delegate-cursor-agent"
    assert manifest.kind == "standalone"
    assert manifest.provides_tools == [TOOL_NAME]
    import yaml

    raw = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert raw.get("platforms") == ["linux", "macos"]
    assert raw.get("author") == "QuixThe2nd"


def test_register_registers_declared_tool_with_contract():
    from hermes_cli.plugins import PluginContext, PluginManager
    from tools.registry import registry

    mgr = PluginManager()
    manifest = mgr._parse_manifest(
        PLUGIN_DIR / "plugin.yaml",
        PLUGIN_DIR,
        source="bundled",
        prefix="",
    )
    assert manifest is not None
    ctx = PluginContext(manifest, mgr)
    mod = _load_plugin_module()
    mod.register(ctx)

    entry = registry.get_entry(TOOL_NAME)
    assert entry is not None
    assert entry.toolset == "delegation"
    assert entry.schema["name"] == TOOL_NAME
    assert entry.check_fn is not None
    assert entry.emoji == "🖥️"
    assert registry.get_max_result_size(TOOL_NAME) == 100_000

    mgr.unload(manifest.name)
    assert registry.get_entry(TOOL_NAME) is None


def test_doctor_validates_and_cleans_up():
    from hermes_cli.plugin_dev import doctor_plugin
    from tools.registry import registry

    before_policy = dict(registry._plugin_override_policy)
    before_modules = {
        name
        for name in sys.modules
        if name == "hermes_plugins" or name.startswith("hermes_plugins.")
    }

    report = doctor_plugin(PLUGIN_DIR)

    assert report.ok, report.format_text()
    assert report.manifest is not None
    assert report.manifest.provides_tools == [TOOL_NAME]
    assert report.registered_tools == (TOOL_NAME,)
    assert registry.get_entry(TOOL_NAME) is None
    assert registry.get_max_result_size(TOOL_NAME) == 100_000
    assert registry._plugin_override_policy == before_policy
    after_modules = {
        name
        for name in sys.modules
        if name == "hermes_plugins" or name.startswith("hermes_plugins.")
    }
    assert after_modules == before_modules


def test_hermes_home_and_run_logs_stay_under_pytest_temp(monkeypatch, _isolate_env):
    from hermes_constants import get_hermes_home
    from delegate_cursor_agent import tool as cursor_agent_tool

    isolated_home = _isolate_env.resolve()
    assert get_hermes_home().resolve() == isolated_home
    assert isolated_home != PRODUCTION_HERMES_HOME

    before_prod = (
        {path.resolve(): path.stat().st_mtime_ns for path in PRODUCTION_RUNS_DIR.glob("*.jsonl")}
        if PRODUCTION_RUNS_DIR.is_dir()
        else {}
    )

    _MinimalStreamingPopen.instances.clear()
    monkeypatch.setattr(
        "delegate_cursor_agent.tool.resolve_cursor_agent_binary",
        lambda: "/usr/bin/agent",
    )
    monkeypatch.setattr(
        "delegate_cursor_agent.tool.subprocess.Popen",
        _MinimalStreamingPopen,
    )

    workdir = _isolate_env / "work"
    workdir.mkdir()
    result = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="hermetic home regression",
            workdir=str(workdir),
        )
    )

    log_path = Path(result["log_path"]).resolve()
    assert log_path.is_file()
    assert isolated_home in log_path.parents
    assert PRODUCTION_HERMES_HOME not in log_path.parents

    after_prod = (
        {path.resolve(): path.stat().st_mtime_ns for path in PRODUCTION_RUNS_DIR.glob("*.jsonl")}
        if PRODUCTION_RUNS_DIR.is_dir()
        else {}
    )
    assert after_prod == before_prod


def test_entrypoint_fails_closed_when_tool_import_breaks(monkeypatch):
    """Broken relative tool import must propagate; no unrelated top-level fallback."""
    import importlib.util

    decoy = types.ModuleType("delegate_cursor_agent")
    decoy.CURSOR_AGENT_SCHEMA = {"name": "DECOY"}
    monkeypatch.setitem(sys.modules, "delegate_cursor_agent", decoy)

    real_import = builtins.__import__

    def _blocking_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level > 0 and name.endswith("delegate_cursor_agent.tool"):
            raise ImportError("simulated broken internal dependency")
        if level > 0 and name.endswith("delegate_cursor_agent") and fromlist and "tool" in fromlist:
            raise ImportError("simulated broken internal dependency")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    module_name = "hermes_plugins.delegate_cursor_agent_import_fail_test"
    sys.modules.pop(module_name, None)

    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.delegate_cursor_agent"
    mod.__path__ = [str(PLUGIN_DIR)]

    with pytest.raises(ImportError, match="simulated broken internal dependency"):
        spec.loader.exec_module(mod)

    assert getattr(mod, "CURSOR_AGENT_SCHEMA", {}).get("name") != "DECOY"
    assert not hasattr(mod, "register")
