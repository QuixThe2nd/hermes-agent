"""Tests for the bundled grievances plugin."""

from __future__ import annotations

import importlib
import importlib.util
import json
import stat
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


@pytest.fixture
def grievances_module():
    repo_root = Path(__file__).resolve().parents[3]
    plugin_dir = repo_root / "plugins" / "grievances"
    module_name = "grievances_plugin_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "__init__.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def token_env(_isolate_env):
    env_path = _isolate_env / ".env"
    env_path.write_text("DISCORD_BOT_TOKEN=test-bot-token\n", encoding="utf-8")
    return env_path


class MockDiscordRouter:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.guilds: List[Dict[str, str]] = [{"id": "guild-1", "name": "Test Guild"}]
        self.channel_counter = 0
        self.message_counter = 0
        self.fail_pin = False

    def __call__(self, request):
        self.calls.append(
            {
                "method": request.method,
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": request.data.decode("utf-8") if request.data else None,
            }
        )
        url = request.full_url
        method = request.method

        if method == "GET" and url.endswith("/users/@me/guilds"):
            return self._response(self.guilds)

        if method == "POST" and "/guilds/" in url and url.endswith("/channels"):
            self.channel_counter += 1
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            return self._response(
                {
                    "id": f"channel-{self.channel_counter}",
                    "name": body.get("name", "ai-grievances"),
                    "guild_id": url.split("/guilds/")[1].split("/")[0],
                }
            )

        if method == "POST" and "/channels/" in url and url.endswith("/messages"):
            self.message_counter += 1
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            payload = {"id": f"msg-{self.message_counter}"}
            if "embeds" in body:
                payload["embeds"] = body["embeds"]
            if "content" in body:
                payload["content"] = body["content"]
            return self._response(payload)

        if method == "PUT" and "/pins/" in url:
            if self.fail_pin:
                raise urllib.error.HTTPError(
                    url, 403, "Forbidden", hdrs=None, fp=BytesIO(b'{"message": "no pin"}')
                )
            return self._response({})

        raise AssertionError(f"unexpected request: {method} {url}")

    @staticmethod
    def _response(payload, status: int = 200):
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp


@pytest.fixture
def discord_router(monkeypatch):
    router = MockDiscordRouter()
    monkeypatch.setattr("urllib.request.urlopen", router)
    return router


def _call(grievances_module, args: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(grievances_module.handle_grievance(args))


class TestMessageFormat:
    def test_compose_message(self, grievances_module):
        text = grievances_module._compose_message(
            3, "technical", "notable", "The deploy script lies.", "Fix the README."
        )
        assert text == (
            "**📋 Grievance #3 — technical [notable]**\n"
            "The deploy script lies.\n"
            "*Suggested remediation:* Fix the README."
        )


class TestSplitting:
    def test_split_on_paragraph_boundaries(self, grievances_module):
        header = "**📋 Grievance #1 — process [whinge]**\n"
        footer = "\n*Suggested remediation:* do better"
        body = "A" * 1200 + "\n\n" + "B" * 1200
        parts = grievances_module._split_message(header + body + footer)
        assert len(parts) >= 2
        assert all(len(part) <= grievances_module._MAX_MESSAGE_LEN for part in parts)
        assert "".join(parts).replace("\n\n", "") == (header + body + footer).replace("\n\n", "")

    def test_hard_wrap_without_paragraph_boundaries(self, grievances_module):
        chunk = "x" * 4000
        parts = grievances_module._split_message(chunk)
        assert len(parts) >= 3
        assert all(len(part) <= grievances_module._MAX_MESSAGE_LEN for part in parts)
        assert "".join(parts) == chunk


class TestCheckRequirements:
    def test_missing_env(self, grievances_module, _isolate_env):
        assert grievances_module.check_requirements() is False

    def test_empty_token(self, grievances_module, _isolate_env):
        (_isolate_env / ".env").write_text("DISCORD_BOT_TOKEN=\n", encoding="utf-8")
        assert grievances_module.check_requirements() is False

    def test_valid_token(self, grievances_module, token_env):
        assert grievances_module.check_requirements() is True


class TestCounterPersistence:
    def test_counter_increments_and_persists(
        self, grievances_module, token_env, discord_router, _isolate_env
    ):
        state_path = _isolate_env / "grievances" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-existing",
                    "channel_name": "ai-grievances",
                    "welcome_message_id": "msg-0",
                    "counter": 4,
                }
            ),
            encoding="utf-8",
        )

        first = _call(
            grievances_module,
            {
                "action": "file",
                "category": "personal",
                "grievance": "First",
                "remediation": "Fix one",
            },
        )
        second = _call(
            grievances_module,
            {
                "action": "file",
                "category": "vibe",
                "grievance": "Second",
                "remediation": "Fix two",
            },
        )

        assert first["success"] is True
        assert first["grievance_number"] == 5
        assert second["success"] is True
        assert second["grievance_number"] == 6

        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["counter"] == 6


class TestCorruptStateRecovery:
    def test_corrupt_state_treated_as_unprovisioned(
        self, grievances_module, token_env, discord_router, _isolate_env
    ):
        state_path = _isolate_env / "grievances" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{not-json", encoding="utf-8")

        result = _call(
            grievances_module,
            {
                "action": "file",
                "category": "process",
                "grievance": "Broken state",
                "remediation": "Recreate channel",
            },
        )

        assert result["success"] is True
        assert result["grievance_number"] == 1
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["channel_id"] == "channel-1"
        assert saved["counter"] == 1


class TestHttpErrors:
    def test_http_error_returns_failure_without_raising(
        self, grievances_module, token_env, monkeypatch, _isolate_env
    ):
        state_path = _isolate_env / "grievances" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-existing",
                    "channel_name": "ai-grievances",
                    "welcome_message_id": "msg-0",
                    "counter": 0,
                }
            ),
            encoding="utf-8",
        )

        def _raise_http(request):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", hdrs=None, fp=BytesIO(b"{}")
            )

        monkeypatch.setattr("urllib.request.urlopen", _raise_http)

        result = _call(
            grievances_module,
            {
                "action": "file",
                "category": "technical",
                "grievance": "boom",
                "remediation": "retry",
            },
        )
        assert result == {"success": False, "error": "HTTP error: 500"}


class TestSetupProvisioning:
    def test_happy_path_persists_state(
        self, grievances_module, token_env, discord_router, _isolate_env
    ):
        result = _call(grievances_module, {"action": "setup"})

        assert result["success"] is True
        assert result["guild_id"] == "guild-1"
        assert result["channel_id"] == "channel-1"
        assert result["channel_name"] == "ai-grievances"
        assert result["welcome_message_id"] == "msg-1"

        state_path = _isolate_env / "grievances" / "state.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["guild_id"] == "guild-1"
        assert saved["channel_id"] == "channel-1"
        assert saved["welcome_message_id"] == "msg-1"
        assert saved["counter"] == 0
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

        methods = [call["method"] for call in discord_router.calls]
        assert methods == ["GET", "POST", "POST", "PUT"]
        assert "Authorization" in discord_router.calls[0]["headers"]
        assert discord_router.calls[0]["headers"]["Authorization"] == "Bot test-bot-token"
        assert "test-bot-token" not in json.dumps(result)

        welcome_body = json.loads(discord_router.calls[2]["body"])
        assert "embeds" in welcome_body
        assert welcome_body["embeds"][0]["title"] == "📋 AI Grievances"

    def test_single_guild_auto_detect(self, grievances_module, token_env, discord_router):
        discord_router.guilds = [{"id": "solo-guild", "name": "Only Server"}]
        result = _call(grievances_module, {"action": "setup"})
        assert result["success"] is True
        assert result["guild_id"] == "solo-guild"

    def test_multi_guild_error_lists_guilds(
        self, grievances_module, token_env, discord_router
    ):
        discord_router.guilds = [
            {"id": "g1", "name": "Alpha"},
            {"id": "g2", "name": "Beta"},
        ]
        result = _call(grievances_module, {"action": "setup"})
        assert result["success"] is False
        assert "multiple guilds" in result["error"]
        assert result["guilds"] == [
            {"id": "g1", "name": "Alpha"},
            {"id": "g2", "name": "Beta"},
        ]

    def test_zero_guild_error(self, grievances_module, token_env, discord_router):
        discord_router.guilds = []
        result = _call(grievances_module, {"action": "setup"})
        assert result == {"success": False, "error": "bot is not in any guild"}

    def test_setup_idempotent_without_force(
        self, grievances_module, token_env, discord_router, _isolate_env
    ):
        first = _call(grievances_module, {"action": "setup"})
        assert first["success"] is True
        calls_after_first = len(discord_router.calls)

        second = _call(grievances_module, {"action": "setup"})
        assert second["success"] is True
        assert second["already_provisioned"] is True
        assert len(discord_router.calls) == calls_after_first

    def test_force_reprovision(
        self, grievances_module, token_env, discord_router, _isolate_env
    ):
        _call(grievances_module, {"action": "setup", "channel_name": "first-channel"})
        _call(
            grievances_module,
            {"action": "setup", "channel_name": "second-channel", "force": True},
        )

        create_calls = [
            json.loads(call["body"])
            for call in discord_router.calls
            if call["method"] == "POST" and call["url"].endswith("/channels")
        ]
        assert [body["name"] for body in create_calls] == ["first-channel", "second-channel"]

        saved = json.loads((_isolate_env / "grievances" / "state.json").read_text())
        assert saved["channel_name"] == "second-channel"
        assert saved["channel_id"] == "channel-2"

    def test_pin_failure_tolerated(
        self, grievances_module, token_env, discord_router, _isolate_env
    ):
        discord_router.fail_pin = True
        result = _call(grievances_module, {"action": "setup"})
        assert result["success"] is True
        assert "warning" in result
        assert "pin failed" in result["warning"]


class TestFileAutoProvision:
    def test_file_auto_provisions_when_unprovisioned(
        self, grievances_module, token_env, discord_router, _isolate_env
    ):
        result = _call(
            grievances_module,
            {
                "category": "technical",
                "grievance": "Needs a home",
                "remediation": "Create the channel",
            },
        )

        assert result["success"] is True
        assert result["action"] == "file"
        assert result["grievance_number"] == 1
        assert result["channel_id"] == "channel-1"
        assert len(result["channel_message_ids"]) == 1

        posted = [
            json.loads(call["body"])["content"]
            for call in discord_router.calls
            if call["method"] == "POST"
            and "/messages" in call["url"]
            and call["body"]
            and '"content"' in call["body"]
        ]
        assert posted[0].startswith("**📋 Grievance #1 — technical [notable]**")
