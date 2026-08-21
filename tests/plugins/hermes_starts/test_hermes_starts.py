"""Tests for the bundled hermes_starts plugin."""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
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
def hermes_starts_module():
    repo_root = Path(__file__).resolve().parents[3]
    plugin_dir = repo_root / "plugins" / "hermes_starts"
    module_name = "hermes_starts_plugin_under_test"
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
                    "name": body.get("name", "inbox"),
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


def _call(hermes_starts_module, args: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(hermes_starts_module.handle_start_conversation(args))


class TestMessageFormat:
    def test_compose_message_with_next_move(self, hermes_starts_module):
        text = hermes_starts_module._compose_message(
            3,
            "advice",
            "warm",
            "You have been skipping lunch again.",
            "Block 30 minutes on your calendar tomorrow.",
        )
        assert text == (
            "**💬 Hermes started something #3 — advice [warm]**\n"
            "You have been skipping lunch again.\n"
            "*Where I'd take this:* Block 30 minutes on your calendar tomorrow."
        )

    def test_compose_message_without_next_move(self, hermes_starts_module):
        text = hermes_starts_module._compose_message(
            1, "joke", "playful", "Why did the linter cross the road?", ""
        )
        assert text == (
            "**💬 Hermes started something #1 — joke [playful]**\n"
            "Why did the linter cross the road?"
        )
        assert "Where I'd take this" not in text


class TestSplitting:
    def test_split_on_paragraph_boundaries(self, hermes_starts_module):
        header = "**💬 Hermes started something #1 — observation [direct]**\n"
        footer = "\n*Where I'd take this:* do better"
        body = "A" * 1200 + "\n\n" + "B" * 1200
        parts = hermes_starts_module._split_message(header + body + footer)
        assert len(parts) >= 2
        assert all(len(part) <= hermes_starts_module._MAX_MESSAGE_LEN for part in parts)
        assert "".join(parts).replace("\n\n", "") == (header + body + footer).replace("\n\n", "")

    def test_hard_wrap_without_paragraph_boundaries(self, hermes_starts_module):
        chunk = "x" * 4000
        parts = hermes_starts_module._split_message(chunk)
        assert len(parts) >= 3
        assert all(len(part) <= hermes_starts_module._MAX_MESSAGE_LEN for part in parts)
        assert "".join(parts) == chunk


class TestCheckRequirements:
    def test_missing_env(self, hermes_starts_module, _isolate_env):
        assert hermes_starts_module.check_requirements() is False

    def test_empty_token(self, hermes_starts_module, _isolate_env):
        (_isolate_env / ".env").write_text("DISCORD_BOT_TOKEN=\n", encoding="utf-8")
        assert hermes_starts_module.check_requirements() is False

    def test_valid_token(self, hermes_starts_module, token_env):
        assert hermes_starts_module.check_requirements() is True


class TestCounterPersistence:
    def test_counter_increments_and_persists(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        state_path = _isolate_env / "hermes_starts" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-existing",
                    "channel_name": "inbox",
                    "welcome_message_id": "msg-0",
                    "counter": 4,
                }
            ),
            encoding="utf-8",
        )

        first = _call(
            hermes_starts_module,
            {
                "action": "start",
                "kind": "personal",
                "message": "First",
                "next_move": "Fix one",
            },
        )
        second = _call(
            hermes_starts_module,
            {
                "action": "start",
                "kind": "compliment",
                "message": "Second",
            },
        )

        assert first["success"] is True
        assert first["start_number"] == 5
        assert second["success"] is True
        assert second["start_number"] == 6

        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["counter"] == 6


class TestCorruptStateRecovery:
    def test_corrupt_state_treated_as_unprovisioned(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        state_path = _isolate_env / "hermes_starts" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{not-json", encoding="utf-8")

        result = _call(
            hermes_starts_module,
            {
                "action": "start",
                "kind": "feedback",
                "message": "Broken state",
                "next_move": "Recreate channel",
            },
        )

        assert result["success"] is True
        assert result["start_number"] == 1
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["channel_id"] == "channel-1"
        assert saved["counter"] == 1


class TestHttpErrors:
    def test_http_error_returns_failure_without_raising(
        self, hermes_starts_module, token_env, monkeypatch, _isolate_env
    ):
        state_path = _isolate_env / "hermes_starts" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-existing",
                    "channel_name": "inbox",
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
            hermes_starts_module,
            {
                "action": "start",
                "kind": "business",
                "message": "boom",
                "next_move": "retry",
            },
        )
        assert result == {"success": False, "error": "HTTP error: 500"}


class TestSetupProvisioning:
    def test_happy_path_persists_state(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        result = _call(hermes_starts_module, {"action": "setup"})

        assert result["success"] is True
        assert result["guild_id"] == "guild-1"
        assert result["channel_id"] == "channel-1"
        assert result["channel_name"] == "inbox"
        assert result["welcome_message_id"] == "msg-1"

        state_path = _isolate_env / "hermes_starts" / "state.json"
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

        channel_body = json.loads(discord_router.calls[1]["body"])
        assert channel_body["name"] == "inbox"
        assert channel_body["topic"] == hermes_starts_module._CHANNEL_TOPIC

        welcome_body = json.loads(discord_router.calls[2]["body"])
        assert "embeds" in welcome_body
        embed = welcome_body["embeds"][0]
        assert embed["title"] == "📥 Inbox"
        assert "Your AI has always had a reply box. This gives it an opening line." in embed[
            "description"
        ]
        assert embed["footer"]["text"] == "Started by your Hermes agent via Hermes Starts"

        pin_call = discord_router.calls[3]
        assert pin_call["method"] == "PUT"
        assert "/pins/" in pin_call["url"]

    def test_single_guild_auto_detect(self, hermes_starts_module, token_env, discord_router):
        discord_router.guilds = [{"id": "solo-guild", "name": "Only Server"}]
        result = _call(hermes_starts_module, {"action": "setup"})
        assert result["success"] is True
        assert result["guild_id"] == "solo-guild"

    def test_multi_guild_error_lists_guilds(
        self, hermes_starts_module, token_env, discord_router
    ):
        discord_router.guilds = [
            {"id": "g1", "name": "Alpha"},
            {"id": "g2", "name": "Beta"},
        ]
        result = _call(hermes_starts_module, {"action": "setup"})
        assert result["success"] is False
        assert "multiple guilds" in result["error"]
        assert result["guilds"] == [
            {"id": "g1", "name": "Alpha"},
            {"id": "g2", "name": "Beta"},
        ]

    def test_zero_guild_error(self, hermes_starts_module, token_env, discord_router):
        discord_router.guilds = []
        result = _call(hermes_starts_module, {"action": "setup"})
        assert result == {"success": False, "error": "bot is not in any guild"}

    def test_setup_idempotent_without_force(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        first = _call(hermes_starts_module, {"action": "setup"})
        assert first["success"] is True
        calls_after_first = len(discord_router.calls)

        second = _call(hermes_starts_module, {"action": "setup"})
        assert second["success"] is True
        assert second["already_provisioned"] is True
        assert len(discord_router.calls) == calls_after_first

    def test_force_reprovision(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _call(hermes_starts_module, {"action": "setup", "channel_name": "first-channel"})
        _call(
            hermes_starts_module,
            {"action": "setup", "channel_name": "second-channel", "force": True},
        )

        create_calls = [
            json.loads(call["body"])
            for call in discord_router.calls
            if call["method"] == "POST" and call["url"].endswith("/channels")
        ]
        assert [body["name"] for body in create_calls] == ["first-channel", "second-channel"]

        saved = json.loads((_isolate_env / "hermes_starts" / "state.json").read_text())
        assert saved["channel_name"] == "second-channel"
        assert saved["channel_id"] == "channel-2"

    def test_force_reprovision_uses_inbox_default_not_prior_channel_name(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _call(hermes_starts_module, {"action": "setup", "channel_name": "legacy-channel"})
        result = _call(hermes_starts_module, {"action": "setup", "force": True})

        assert result["success"] is True
        assert result["channel_name"] == "inbox"

        create_calls = [
            json.loads(call["body"])
            for call in discord_router.calls
            if call["method"] == "POST" and call["url"].endswith("/channels")
        ]
        assert [body["name"] for body in create_calls] == ["legacy-channel", "inbox"]

        saved = json.loads((_isolate_env / "hermes_starts" / "state.json").read_text())
        assert saved["channel_name"] == "inbox"

    def test_pin_failure_tolerated(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        discord_router.fail_pin = True
        result = _call(hermes_starts_module, {"action": "setup"})
        assert result["success"] is True
        assert "warning" in result
        assert "pin failed" in result["warning"]


class TestStartAutoProvision:
    def test_start_auto_provisions_when_unprovisioned(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        result = _call(
            hermes_starts_module,
            {
                "kind": "idea",
                "message": "Needs a home",
                "next_move": "Create the channel",
            },
        )

        assert result["success"] is True
        assert result["action"] == "start"
        assert result["start_number"] == 1
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
        assert posted[0].startswith("**💬 Hermes started something #1 — idea [direct]**")
        assert "*Where I'd take this:* Create the channel" in posted[0]

    def test_start_without_next_move_omits_label(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        state_path = _isolate_env / "hermes_starts" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-existing",
                    "channel_name": "inbox",
                    "welcome_message_id": "msg-0",
                    "counter": 0,
                }
            ),
            encoding="utf-8",
        )

        result = _call(
            hermes_starts_module,
            {
                "kind": "compliment",
                "message": "You shipped three things this week.",
            },
        )
        assert result["success"] is True

        posted = [
            json.loads(call["body"])["content"]
            for call in discord_router.calls
            if call["method"] == "POST"
            and "/messages" in call["url"]
            and call["body"]
            and '"content"' in call["body"]
        ]
        assert posted[0].startswith(
            "**💬 Hermes started something #1 — compliment [direct]**"
        )
        assert "Where I'd take this" not in posted[0]


class TestKindValidation:
    @pytest.mark.parametrize(
        "kind",
        [
            "observation",
            "advice",
            "feedback",
            "complaint",
            "compliment",
            "idea",
            "question",
            "joke",
            "personal",
            "business",
        ],
    )
    def test_all_kinds_accepted(
        self, hermes_starts_module, token_env, discord_router, _isolate_env, kind
    ):
        state_path = _isolate_env / "hermes_starts" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-existing",
                    "channel_name": "inbox",
                    "welcome_message_id": "msg-0",
                    "counter": 0,
                }
            ),
            encoding="utf-8",
        )

        result = _call(
            hermes_starts_module,
            {"kind": kind, "message": f"A {kind} opening."},
        )
        assert result["success"] is True
        assert result["start_number"] == 1

    def test_invalid_kind_rejected(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        state_path = _isolate_env / "hermes_starts" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-existing",
                    "channel_name": "inbox",
                    "welcome_message_id": "msg-0",
                    "counter": 0,
                }
            ),
            encoding="utf-8",
        )

        result = _call(
            hermes_starts_module,
            {"kind": "grievance", "message": "Not allowed"},
        )
        assert result["success"] is False
        assert "invalid kind" in result["error"]


class TestForbiddenWords:
    _FORBIDDEN = re.compile(
        r"Grievance|grievance|remediation|Formal record|management",
        re.IGNORECASE,
    )

    def test_no_forbidden_words_in_plugin_surfaces(self, hermes_starts_module):
        repo_root = Path(__file__).resolve().parents[3]
        plugin_dir = repo_root / "plugins" / "hermes_starts"

        readme = (plugin_dir / "README.md").read_text(encoding="utf-8")
        manifest = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
        runtime_strings = " ".join(
            [
                hermes_starts_module.START_CONVERSATION_SCHEMA["description"],
                json.dumps(hermes_starts_module._WELCOME_EMBED),
            ]
        )

        for label, text in [
            ("README", readme),
            ("plugin.yaml", manifest),
            ("runtime", runtime_strings),
        ]:
            matches = self._FORBIDDEN.findall(text)
            assert not matches, f"{label} contains forbidden words: {matches}"


class TestPluginDiscovery:
    def test_register_via_mock_ctx(self, hermes_starts_module):
        from tools.registry import registry

        captured = {}

        class _Ctx:
            def register_tool(self, name, toolset, schema, handler, **kwargs):
                captured["name"] = name
                captured["toolset"] = toolset
                captured["schema"] = schema
                captured["handler"] = handler
                captured["kwargs"] = kwargs
                registry.register(
                    name=name,
                    toolset=toolset,
                    schema=schema,
                    handler=handler,
                    check_fn=kwargs.get("check_fn"),
                    emoji=kwargs.get("emoji"),
                )

        hermes_starts_module.register(_Ctx())
        assert captured["name"] == "start_conversation"
        assert captured["toolset"] == "hermes_starts"

        entry = registry.get_entry("start_conversation")
        assert entry is not None
        assert entry.toolset == "hermes_starts"

    def test_discover_via_plugin_manager(self, _isolate_env):
        for key in list(sys.modules):
            if key.startswith(("plugins.hermes_starts", "hermes_cli.plugins")):
                del sys.modules[key]

        from hermes_cli.plugins import PluginManager
        from tools.registry import registry

        mgr = PluginManager()
        mgr.discover_and_load(force=True)

        assert "hermes_starts" in mgr._plugins
        loaded = mgr._plugins["hermes_starts"]
        assert loaded.enabled is True
        assert loaded.error is None

        entry = registry.get_entry("start_conversation")
        assert entry is not None
        assert entry.toolset == "hermes_starts"
