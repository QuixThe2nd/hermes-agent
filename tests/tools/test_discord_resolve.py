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
    def test_posts_embed_and_reactions(self):
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            return {"id": "msg-1"}

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("propose", "123", "Fixed the thing"))

        assert result["success"] is True
        assert result["message_id"] == "msg-1"
        method, path, body = calls[0]
        assert method == "POST"
        assert path == "/channels/123/messages"
        embed = body["embeds"][0]
        assert "Fixed the thing" in embed["description"]
        assert "never delete" in embed["footer"]["text"]
        reaction_calls = [c for c in calls if c[0] == "PUT"]
        assert len(reaction_calls) == 2
        assert all("/reactions/" in c[1] and c[1].endswith("/@me") for c in reaction_calls)

    def test_reaction_failure_not_fatal(self):
        def fake_request(method, path, token, params=None, body=None, timeout=15):
            if method == "PUT":
                raise drt.DiscordAPIError(403, "Missing Permissions")
            return {"id": "msg-1"}

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("propose", "123", "x"))

        assert result["success"] is True

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


def _resolve_embed_message(footer="hermes ticket resolution • archive only, never delete"):
    return {
        "id": "msg-1",
        "embeds": [{
            "title": "✅ Ticket resolved?",
            "description": "Fixed the thing",
            "footer": {"text": footer},
        }],
    }


class TestReactionHandler:
    def test_tick_edits_embed_then_archives(self):
        calls = []
        state = {"archived": False}

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            assert method != "DELETE", "reaction handler must never DELETE"
            if method == "GET" and path.endswith("/messages/msg-1"):
                return _resolve_embed_message()
            if method == "GET" and path == "/channels/123":
                return _thread_channel(archived=state["archived"])
            if method == "PATCH" and path == "/channels/123":
                state["archived"] = True
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "✅")

        assert result == {"acted": True, "decision": "closed", "archived": True}
        embed_edit = next(c for c in calls if c[0] == "PATCH" and c[1].endswith("/messages/msg-1"))
        assert "• closed" in embed_edit[2]["embeds"][0]["footer"]["text"]
        assert ("PATCH", "/channels/123", {"archived": True}) in calls

    def test_cross_edits_embed_and_does_not_archive(self):
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            assert not (method == "PATCH" and path == "/channels/123"), "cross must not archive"
            if method == "GET":
                return _resolve_embed_message()
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "❌")

        assert result == {"acted": True, "decision": "kept_open"}
        embed_edit = next(c for c in calls if c[0] == "PATCH")
        assert "• kept open" in embed_edit[2]["embeds"][0]["footer"]["text"]

    def test_ignores_unrelated_emoji(self):
        assert drt.handle_resolve_reaction("123", "msg-1", "🔥")["acted"] is False

    def test_ignores_non_resolve_message(self):
        def fake_request(method, path, token, params=None, body=None, timeout=15):
            return {"id": "msg-1", "embeds": [], "content": "hello"}

        with patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "✅")

        assert result == {"acted": False, "reason": "not_a_resolve_embed"}

    def test_idempotent_after_decision(self):
        def fake_request(method, path, token, params=None, body=None, timeout=15):
            if method == "GET" and path.endswith("/messages/msg-1"):
                return _resolve_embed_message(footer="hermes ticket resolution • closed")
            raise AssertionError("no further calls expected")

        with patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "✅")

        assert result == {"acted": False, "reason": "already_decided"}


class TestRegistration:
    def test_registered_in_discord_toolset(self):
        from tools.registry import registry
        from toolsets import TOOLSETS

        assert "resolve_ticket" in TOOLSETS["discord"]["tools"]
        entry = registry.get_entry("resolve_ticket")
        assert entry is not None
        assert entry.toolset == "discord"
