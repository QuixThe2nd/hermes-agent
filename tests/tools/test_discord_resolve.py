"""Tests for tools/discord_resolve_tool.py (resolve_ticket).

The hard safety contract under test: closing a ticket archives the thread
and NEVER issues a DELETE, and non-thread channels are refused.
"""

import json
from unittest.mock import patch

import pytest

from tools import discord_resolve_tool as drt


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(drt, "_get_bot_token", lambda: "fake-token")


def _thread_channel(archived=False, ctype=11):
    return {
        "id": "123",
        "type": ctype,
        "thread_metadata": {"archived": archived},
    }


class TestPropose:
    def test_posts_embed(self):
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            return {"id": "msg-1"}

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("propose", "123", "Fixed the thing"))

        assert result["success"] is True
        assert result["message_id"] == "msg-1"
        assert len(calls) == 1
        method, path, body = calls[0]
        assert method == "POST"
        assert path == "/channels/123/messages"
        embed = body["embeds"][0]
        assert "Fixed the thing" in embed["description"]
        assert "never delete" in embed["footer"]["text"]

    def test_missing_channel_id(self):
        result = json.loads(drt.resolve_ticket("propose", "", "x"))
        assert result.get("success") is not True


class TestClose:
    def test_archives_thread_and_never_deletes(self):
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            assert method != "DELETE", "resolve_ticket must never DELETE"
            if method == "GET":
                return _thread_channel(archived=any(
                    m == "PATCH" for m, _, _ in calls
                ))
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("close", "123"))

        assert result["success"] is True
        assert result["archived"] is True
        assert result["deleted"] is False
        patch_calls = [c for c in calls if c[0] == "PATCH"]
        assert patch_calls == [("PATCH", "/channels/123", {"archived": True})]

    def test_refuses_non_thread(self):
        def fake_request(method, path, token, params=None, body=None, timeout=15):
            if method == "GET":
                return _thread_channel(ctype=0)
            raise AssertionError("no write calls allowed for non-thread")

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("close", "123"))

        assert result.get("success") is not True
        assert "not a thread" in result.get("error", result.get("message", ""))

    def test_idempotent_when_already_archived(self):
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path))
            return _thread_channel(archived=True)

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("close", "123"))

        assert result["success"] is True
        assert result["already_archived"] is True
        assert all(m == "GET" for m, _ in calls)

    def test_farewell_failure_still_archives(self):
        state = {"archived": False}

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            if method == "GET":
                return _thread_channel(archived=state["archived"])
            if method == "POST":
                raise drt.DiscordAPIError(403, "Missing Access")
            if method == "PATCH":
                state["archived"] = True
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("close", "123"))

        assert result["success"] is True
        assert result["archived"] is True

    def test_unknown_action(self):
        result = json.loads(drt.resolve_ticket("explode", "123"))
        assert result.get("success") is not True


class TestRegistration:
    def test_registered_in_discord_toolset(self):
        from tools.registry import registry
        from toolsets import TOOLSETS

        assert "resolve_ticket" in TOOLSETS["discord"]["tools"]
        entry = registry.get_entry("resolve_ticket")
        assert entry is not None
        assert entry.toolset == "discord"
