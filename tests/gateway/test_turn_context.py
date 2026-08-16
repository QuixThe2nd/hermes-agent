"""Unit tests for the TurnContext/TurnRunner seam extracted from
``GatewayRunner._run_agent_inner`` (gateway/turn_context.py + gateway/run.py).

The extraction contract: the closure bodies moved onto ``TurnRunner`` methods
byte-identically (modulo local -> ctx.field rewrites), with every closed-over
local carried as a ``TurnContext`` field. These tests pin the seam's wiring —
shared mutable containers, no-queue early returns — not the progress behavior
itself (that's covered by test_run_progress_topics.py et al.).
"""

import asyncio
import queue as queue_mod
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def _make_runner(ctx, adapter=None):
    from gateway.run import TurnRunner

    class _StubGatewayRunner:
        def _adapter_for_source(self, source):
            return adapter

    return TurnRunner(_StubGatewayRunner(), ctx)


class TestTurnContext:
    def test_defaults_are_independent_containers(self):
        a, b = TurnContext(), TurnContext()
        a.last_progress_msg[0] = "x"
        a.repeat_count[0] = 3
        a._cleanup_msg_ids.append("1")
        assert b.last_progress_msg == [None]
        assert b.repeat_count == [0]
        assert b._cleanup_msg_ids == []

    def test_shared_containers_visible_to_outer_scope(self):
        # The outer body and the runner share the SAME list objects, so
        # mutation through the ctx is visible to locals captured elsewhere.
        last_progress_msg = [None]
        ctx = TurnContext(last_progress_msg=last_progress_msg)
        ctx.last_progress_msg[0] = "🔍 web_search"
        assert last_progress_msg[0] == "🔍 web_search"


class TestTurnRunner:
    def test_methods_exist_and_bind(self):
        from gateway.run import TurnRunner

        ctx = TurnContext()
        runner = _make_runner(ctx)
        assert callable(runner.progress_callback)
        assert asyncio.iscoroutinefunction(TurnRunner.send_progress_messages)
        assert runner._ctx is ctx

    def test_send_progress_messages_no_queue_returns(self):
        ctx = TurnContext(progress_queue=None)
        runner = _make_runner(ctx)
        assert asyncio.run(runner.send_progress_messages()) is None

    def test_send_progress_messages_no_adapter_returns(self):
        ctx = TurnContext(progress_queue=queue_mod.Queue())
        runner = _make_runner(ctx)  # stub adapter resolver returns None
        assert asyncio.run(runner.send_progress_messages()) is None

    def test_context_injection_uses_tool_progress_queue_with_full_redacted_content(self):
        class _DiscordLikeAdapter:
            MAX_MESSAGE_LENGTH = 2000
            supports_code_blocks = True

        progress_queue = queue_mod.Queue()
        ctx = TurnContext(
            source=SimpleNamespace(chat_id="c1"),
            _run_still_current=lambda: True,
            progress_mode="all",
            tool_progress_enabled=True,
            progress_queue=progress_queue,
        )
        runner = _make_runner(ctx, _DiscordLikeAdapter())
        secret = "ghp_" + ("a" * 36)
        content = (
            "\n\n<memory-context>\n"
            f"token={secret}\n"
            "quoted mention: <@1234567890>\n"
            + ("remembered observation\n" * 300)
            + "</memory-context>"
        )

        runner.progress_callback(
            "context.injected",
            "context",
            None,
            {
                "content": content,
                "injected_chars": len(content) + 2,
                "sources": ["memory"],
            },
        )

        messages = []
        while not progress_queue.empty():
            messages.append(progress_queue.get_nowait())
        assert len(messages) > 1  # 5k-ish context is split for Discord
        assert all(len(message) <= 1936 for message in messages)  # 2000 - safety margin
        rendered = "\n".join(messages)
        assert rendered.startswith(f"🧠 memory context injected (+{len(content) + 2:,} chars)")
        assert "<memory-context>" in rendered
        assert "</memory-context>" in rendered
        assert secret not in rendered
        assert "«redacted:ghp_…»" in rendered
        assert "<@1234567890>" not in rendered
        assert "<@\u200b1234567890>" in rendered
        assert all(message.count("```") == 2 for message in messages)

        from agent.redact import redact_sensitive_text
        from gateway.stream_consumer import escape_code_fences_for_display

        displayed_chunks = []
        for message in messages:
            body = message.split("\n", 1)[1]
            displayed_chunks.append(body[4:-4])  # outer ```\n ... \n```
        expected_display = escape_code_fences_for_display(
            redact_sensitive_text(
                content,
                force=True,
                file_read=True,
                redact_url_credentials=True,
            ).replace("@", "@\u200b")
        )
        assert "".join(displayed_chunks) == expected_display

    def test_context_injection_respects_per_chat_utf16_limit(self):
        from gateway.platforms.base import BasePlatformAdapter, utf16_len

        class _Utf16Adapter(BasePlatformAdapter):
            supports_code_blocks = True
            MAX_MESSAGE_LENGTH = 4096

            def __init__(self):
                pass

            async def connect(self, *, is_reconnect=False):
                return True

            async def disconnect(self):
                return None

            async def send(self, *args, **kwargs):
                return None

            async def get_chat_info(self, chat_id):
                return {}

            def max_message_length_for_chat(self, chat_id):
                return 4096

            def message_len_fn_for_chat(self, chat_id):
                return utf16_len

        progress_queue = queue_mod.Queue()
        ctx = TurnContext(
            source=SimpleNamespace(chat_id="c1"),
            _run_still_current=lambda: True,
            progress_mode="all",
            tool_progress_enabled=True,
            progress_queue=progress_queue,
        )
        runner = _make_runner(ctx, _Utf16Adapter())
        runner.progress_callback(
            "context.injected",
            "context",
            None,
            {
                "content": "😀" * 5000,
                "injected_chars": 5000,
                "sources": ["memory"],
            },
        )

        messages = []
        while not progress_queue.empty():
            messages.append(progress_queue.get_nowait())
        assert len(messages) > 1
        assert all(utf16_len(message) <= 4032 for message in messages)
        displayed_chunks = []
        for message in messages:
            body = message.split("\n", 1)[1]
            displayed_chunks.append(body[4:-4])
        assert "".join(displayed_chunks) == "😀" * 5000

    def test_context_injection_respects_duck_typed_per_chat_utf16_limit(self):
        from gateway.platforms.base import utf16_len

        class _DuckUtf16Adapter:
            supports_code_blocks = True
            MAX_MESSAGE_LENGTH = 100

            def max_message_length_for_chat(self, chat_id):
                return 100

            def message_len_fn_for_chat(self, chat_id):
                return utf16_len

        progress_queue = queue_mod.Queue()
        ctx = TurnContext(
            source=SimpleNamespace(chat_id="c1"),
            _run_still_current=lambda: True,
            progress_mode="all",
            tool_progress_enabled=True,
            progress_queue=progress_queue,
        )
        runner = _make_runner(ctx, _DuckUtf16Adapter())
        runner.progress_callback(
            "context.injected",
            "context",
            "",
            {
                "content": "😀" * 200,
                "injected_chars": 200,
                "sources": ["memory"],
            },
        )

        messages = []
        while not progress_queue.empty():
            messages.append(progress_queue.get_nowait())
        assert len(messages) > 1
        assert all(utf16_len(message) <= 100 for message in messages)

    def test_context_injection_suppresses_unrepresentable_unit_budget(self):
        from gateway.platforms.base import utf16_len
        from gateway.run import _split_context_progress_text

        assert _split_context_progress_text("😀", 1, utf16_len) == []

    @pytest.mark.parametrize("message_limit", [1, 8, 40, 64, 128])
    def test_context_injection_respects_even_tiny_adapter_caps(self, message_limit):
        from gateway.run import _format_context_injection_progress

        messages = _format_context_injection_progress(
            content="x" * 334,
            injected_chars=336,
            sources=["source-" + ("y" * 200)],
            message_limit=message_limit,
            supports_code_blocks=True,
        )
        assert messages
        assert all(len(message) <= message_limit for message in messages)

    def test_context_injection_follows_tool_progress_visibility(self):
        progress_queue = queue_mod.Queue()
        ctx = TurnContext(
            source=SimpleNamespace(chat_id="c1"),
            _run_still_current=lambda: True,
            progress_mode="off",
            tool_progress_enabled=False,
            progress_queue=progress_queue,
        )
        adapter = SimpleNamespace(MAX_MESSAGE_LENGTH=2000, supports_code_blocks=True)
        runner = _make_runner(ctx, adapter)
        runner.progress_callback(
            "context.injected",
            "context",
            None,
            {"content": "hidden", "injected_chars": 8, "sources": ["memory"]},
        )
        assert progress_queue.empty()
    def test_normal_response_preserves_compression_exhausted(self):
        """A non-empty exhaustion response must still reach auto-reset consumers."""

        class _ExhaustedAgent:
            def __init__(self, **kwargs):
                self.model = kwargs["model"]
                self.session_id = kwargs["session_id"]
                self.tools = []
                self.context_compressor = SimpleNamespace(
                    last_prompt_tokens=0,
                    context_length=200_000,
                )
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0

            def run_conversation(self, _message, **_kwargs):
                return {
                    "final_response": "Context length exceeded. Cannot compress further.",
                    "failed": True,
                    "compression_exhausted": True,
                    "messages": [],
                }

        gateway_runner = MagicMock()
        gateway_runner.config = SimpleNamespace(streaming=None)
        gateway_runner._provider_routing = {}
        gateway_runner._agent_cache_lock = None
        gateway_runner._agent_cache = {}
        gateway_runner._session_db = None
        gateway_runner._prefill_messages = None
        gateway_runner._pending_model_notes = {}
        gateway_runner._pending_skills_reload_notes = {}
        gateway_runner.session_store._entries = {}
        gateway_runner._get_system_prompt_for_channel.return_value = None
        gateway_runner._resolve_session_agent_runtime.return_value = ("test-model", {})
        gateway_runner._resolve_session_reasoning_config.return_value = None
        gateway_runner._resolve_session_service_tier.return_value = None
        gateway_runner._resolve_turn_agent_config.return_value = {
            "model": "test-model",
            "runtime": {},
        }
        gateway_runner._agent_config_signature.return_value = ("test-signature",)
        gateway_runner._extract_cache_busting_config.return_value = {}
        gateway_runner._refresh_fallback_model.return_value = None
        gateway_runner._consume_pending_native_image_paths.return_value = []
        gateway_runner._consume_pending_turn_sidecar_notes.return_value = []
        gateway_runner._is_telegram_topic_lane.return_value = False
        gateway_runner._is_discord_auto_thread_lane.return_value = False
        gateway_runner._is_relay_discord_channel_lane.return_value = False

        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="test-chat",
            user_id="test-user",
        )
        ctx = TurnContext(
            source=source,
            message="continue",
            history=[],
            session_id="test-session",
            session_key="test-session-key",
            user_config={},
            AIAgent=_ExhaustedAgent,
            resolve_display_setting=lambda *_args: False,
            _run_still_current=lambda: True,
            _hooks_ref=SimpleNamespace(loaded_hooks=False),
        )

        from gateway.run import TurnRunner

        result = TurnRunner(gateway_runner, ctx).run_sync()

        assert result["final_response"] == (
            "Context length exceeded. Cannot compress further."
        )
        assert result["compression_exhausted"] is True
