"""Hermetic pytest harness for delegate-claude-agent plugin tests."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ── Session-scoped HERMES_HOME before test modules import implementation ───
# Plugin-local tests do not inherit repo-root tests/conftest.py. Without this,
# collection-time imports can bind logging and run logs to the operator's real
# ~/.hermes before per-test fixtures run.


def _hermes_home_points_at_production(value: str) -> bool:
    if not value:
        return True
    try:
        resolved = Path(value).expanduser().resolve()
        real_root = (Path.home() / ".hermes").resolve()
    except Exception:
        return True
    if resolved == real_root:
        return True
    return resolved.parent.name == "profiles" and resolved.parent.parent == real_root


if _hermes_home_points_at_production(os.environ.get("HERMES_HOME", "")):
    _SESSION_HERMES_HOME = tempfile.mkdtemp(prefix="hermes-plugin-test-home-")
    os.environ["HERMES_HOME"] = _SESSION_HERMES_HOME
    atexit.register(shutil.rmtree, _SESSION_HERMES_HOME, True)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# ── Credential env-var filter (minimal; mirrors root tests/conftest.py) ─────

_CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
    "_ACCESS_KEY",
    "_SECRET_ACCESS_KEY",
    "_PRIVATE_KEY",
)

_CREDENTIAL_NAMES = frozenset({
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ANTHROPIC_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CURSOR_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GLM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
})


def _looks_like_credential(name: str) -> bool:
    if name in _CREDENTIAL_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES)


def pytest_configure(config):  # noqa: D401 — pytest hook
    config.addinivalue_line(
        "markers",
        "live_system_guard_bypass: bypass the live-system guard for tests "
        "that genuinely need real os.kill / subprocess behaviour.",
    )


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Per-test HERMES_HOME sandbox and credential scrub."""
    for name in list(os.environ.keys()):
        if _looks_like_credential(name):
            monkeypatch.delenv(name, raising=False)

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home
