"""Discord streaming finalization must reply only on the turn-final answer.

Interim streaming previews, commentary, and tool progress stay standalone
(no MessageReference ping). The completed answer is delivered as a fresh
reply because Discord cannot attach a reference via message.edit.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _make_channel():
    sent_messages = []
    delete_calls = []

    async def _send(content, reference=None):
        msg = SimpleNamespace(id=len(sent_messages) + 100, content=content, reference=reference)
        sent_messages.append({"content": content, "reference": reference, "id": msg.id})
        return msg

    partial_messages = {}

    def get_partial_message(mid):
        mid = int(mid)
        if mid not in partial_messages:
            async def _delete():
                delete_calls.append(mid)

            partial_messages[mid] = SimpleNamespace(id=mid, delete=AsyncMock(side_effect=_delete))
        return partial_messages[mid]

    channel = SimpleNamespace(
        id=555,
        send=AsyncMock(side_effect=_send),
        get_partial_message=get_partial_message,
    )
    return channel, sent_messages, delete_calls


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", reply_to_mode="first")
    adapter = DiscordAdapter(config)
    channel, sent_messages, delete_calls = _make_channel()
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
    )
    adapter.truncate_message = lambda content, max_len, **kw: [content]
    adapter.format_message = lambda content: content
    return adapter, channel, sent_messages, delete_calls


def _make_split_capable_adapter():
    """Discord adapter harness that preserves real chunking for overflow splits."""
    config = PlatformConfig(enabled=True, token="test-token", reply_to_mode="first")
    adapter = DiscordAdapter(config)
    sent_messages = []
    delete_calls = []
    edit_calls = []

    async def _send(content, reference=None):
        msg = SimpleNamespace(id=len(sent_messages) + 100, content=content, reference=reference)
        sent_messages.append({"content": content, "reference": reference, "id": msg.id})
        return msg

    partial_messages = {}

    def get_partial_message(mid):
        mid = int(mid)

        async def _edit(*, content):
            edit_calls.append({"id": mid, "content": content})

        async def _delete():
            delete_calls.append(mid)

        if mid not in partial_messages:
            partial_messages[mid] = SimpleNamespace(
                id=mid,
                edit=AsyncMock(side_effect=_edit),
                delete=AsyncMock(side_effect=_delete),
            )
        return partial_messages[mid]

    channel = SimpleNamespace(
        id=555,
        send=AsyncMock(side_effect=_send),
        get_partial_message=get_partial_message,
    )
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
    )
    adapter.format_message = lambda content: content
    return adapter, channel, sent_messages, delete_calls, edit_calls


class TestDiscordReplyReferenceGating:
    @pytest.mark.asyncio
    async def test_streaming_preview_send_has_no_reply_reference(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()

        result = await adapter.send(
            "555",
            "partial answer",
            reply_to="999",
            metadata={"expect_edits": True},
        )

        assert result.success is True
        assert len(sent_messages) == 1
        assert sent_messages[0]["reference"] is None

    @pytest.mark.asyncio
    async def test_interim_commentary_send_has_no_reply_reference(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()

        result = await adapter.send(
            "555",
            "Using browser tool...",
            reply_to="999",
            metadata={"_interim_send": True},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is None

    @pytest.mark.asyncio
    async def test_turn_final_notify_send_has_reply_reference(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()

        result = await adapter.send(
            "555",
            "Final answer",
            reply_to="999",
            metadata={"notify": True},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is not None
        assert sent_messages[0]["reference"].message_id == 999


class TestDiscordStreamConsumerFreshFinal:
    @pytest.mark.asyncio
    async def test_finalize_deletes_preview_and_sends_reply(self):
        adapter, channel, sent_messages, delete_calls = _make_adapter()
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=5,
            cursor="▉",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        consumer.on_delta("Preview text")
        task = __import__("asyncio").create_task(consumer.run())
        await __import__("asyncio").sleep(0.05)
        consumer.finish("Final answer text")
        await task

        assert len(sent_messages) == 2
        assert sent_messages[0]["reference"] is None
        assert sent_messages[1]["reference"] is not None
        assert sent_messages[1]["reference"].message_id == 999
        assert sent_messages[1]["content"] == "Final answer text"
        assert delete_calls == [sent_messages[0]["id"]]
        assert consumer.final_response_sent is True

    @pytest.mark.asyncio
    async def test_commentary_before_tools_has_no_reply_reference(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=5,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        await consumer._send_commentary("I'll check that for you.")
        consumer.on_delta("Done.")
        task = __import__("asyncio").create_task(consumer.run())
        await __import__("asyncio").sleep(0.05)
        consumer.finish("Done.")
        await task

        assert len(sent_messages) == 3
        assert all(msg["reference"] is None for msg in sent_messages[:2])
        assert sent_messages[2]["reference"] is not None
        assert sent_messages[2]["reference"].message_id == 999

    @pytest.mark.asyncio
    async def test_tool_boundary_preamble_has_no_reply_before_turn_final(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=500,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        # Short preamble cut off by a tool call before any interval flush.
        consumer.on_delta("I'll look that up")
        consumer.on_segment_break()
        task = __import__("asyncio").create_task(consumer.run())
        await __import__("asyncio").sleep(0.05)
        consumer.on_delta("The answer is 42.")
        consumer.finish("The answer is 42.")
        await task

        assert len(sent_messages) >= 2
        assert sent_messages[0]["reference"] is None
        assert sent_messages[0]["content"] == "I'll look that up"
        assert sent_messages[-1]["reference"] is not None
        assert sent_messages[-1]["reference"].message_id == 999
        assert sent_messages[-1]["content"] == "The answer is 42."

    @pytest.mark.asyncio
    async def test_failed_fresh_final_does_not_suppress_gateway_reply(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()
        from gateway.platforms.base import SendResult

        send_results = [
            SendResult(success=True, message_id="100"),
            SendResult(success=False, error="fresh final failed"),
        ]

        async def flaky_send(*, chat_id, content, reply_to=None, metadata=None):
            result = send_results.pop(0)
            if result.success:
                ref = None
                if metadata and metadata.get("notify") and reply_to:
                    import discord
                    ref = discord.MessageReference(
                        message_id=int(reply_to),
                        channel_id=555,
                        fail_if_not_exists=False,
                    )
                msg = SimpleNamespace(
                    id=int(result.message_id),
                    content=content,
                    reference=ref,
                )
                sent_messages.append(
                    {"content": content, "reference": ref, "id": msg.id}
                )
            return result

        adapter.send = AsyncMock(side_effect=flaky_send)
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=5,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        consumer.on_delta("Final answer")
        task = __import__("asyncio").create_task(consumer.run())
        await __import__("asyncio").sleep(0.05)
        consumer.finish("Final answer")
        await task

        assert consumer.final_response_sent is False
        assert consumer.final_content_delivered is False
        assert len(sent_messages) == 1
        assert sent_messages[0]["reference"] is None

    @pytest.mark.asyncio
    async def test_split_turn_final_sends_single_fresh_reply_with_reference(self):
        adapter, _channel, sent_messages, delete_calls, edit_calls = (
            _make_split_capable_adapter()
        )
        notify_send_attempts = []
        real_send = adapter.send

        async def tracked_send(*args, **kwargs):
            metadata = kwargs.get("metadata") or {}
            if metadata.get("notify"):
                notify_send_attempts.append(kwargs)
            return await real_send(*args, **kwargs)

        adapter.send = tracked_send
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=1,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        # Past Discord's ~1900-char streaming safe limit → sealed head + tail.
        long_text = ("paragraph " * 250).strip()
        assert len(long_text) > 1900

        task = __import__("asyncio").create_task(consumer.run())
        for offset in range(0, len(long_text), 200):
            consumer.on_delta(long_text[offset : offset + 200])
            await __import__("asyncio").sleep(0.005)
        await __import__("asyncio").sleep(0.1)
        assert consumer._turn_split_delivery is True
        pre_finish_heads = list(sent_messages[:-1])
        tail_id = sent_messages[-1]["id"]
        head_ids = {msg["id"] for msg in pre_finish_heads}
        pre_finish_delete_count = len(delete_calls)
        pre_finish_edit_count = len(edit_calls)
        consumer.finish(long_text)
        await task

        assert consumer.final_response_sent is True
        assert consumer.final_content_delivered is True
        assert len(notify_send_attempts) == 1
        assert notify_send_attempts[0]["reply_to"] == "999"
        assert notify_send_attempts[0]["content"] == long_text
        notify_sends = [msg for msg in sent_messages if msg["reference"] is not None]
        assert len(notify_sends) == 1
        assert notify_sends[0]["reference"].message_id == 999
        post_finish_deletes = delete_calls[pre_finish_delete_count:]
        assert head_ids.issubset(post_finish_deletes)
        assert tail_id in post_finish_deletes
        assert edit_calls[pre_finish_edit_count:] == []

    @pytest.mark.asyncio
    async def test_split_turn_final_fresh_failure_leaves_gateway_fallback(self):
        adapter, _channel, sent_messages, delete_calls, edit_calls = (
            _make_split_capable_adapter()
        )
        from gateway.platforms.base import SendResult

        real_send = adapter.send

        async def flaky_send(*args, **kwargs):
            metadata = kwargs.get("metadata") or {}
            if metadata.get("notify"):
                return SendResult(success=False, error="fresh final failed")
            return await real_send(*args, **kwargs)

        adapter.send = flaky_send
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=1,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        long_text = ("paragraph " * 250).strip()
        assert len(long_text) > 1900

        task = __import__("asyncio").create_task(consumer.run())
        for offset in range(0, len(long_text), 200):
            consumer.on_delta(long_text[offset : offset + 200])
            await __import__("asyncio").sleep(0.005)
        await __import__("asyncio").sleep(0.1)
        assert consumer._turn_split_delivery is True
        head_ids = {msg["id"] for msg in sent_messages[:-1]}
        pre_finish_delete_count = len(delete_calls)
        consumer.finish(long_text)
        await task

        assert consumer.final_response_sent is False
        assert head_ids.isdisjoint(delete_calls[pre_finish_delete_count:])

    @pytest.mark.asyncio
    async def test_split_turn_final_cleanup_failure_still_delivered(self):
        adapter, channel, sent_messages, delete_calls, edit_calls = (
            _make_split_capable_adapter()
        )
        notify_send_attempts = []
        real_send = adapter.send
        real_get_partial = channel.get_partial_message
        failing_head_id = None

        async def tracked_send(*args, **kwargs):
            metadata = kwargs.get("metadata") or {}
            if metadata.get("notify"):
                notify_send_attempts.append(kwargs)
            return await real_send(*args, **kwargs)

        def get_partial_message(mid):
            nonlocal failing_head_id
            partial = real_get_partial(mid)
            if failing_head_id is not None and int(mid) == failing_head_id:
                async def _delete():
                    delete_calls.append(mid)
                    raise RuntimeError("delete failed")

                partial.delete = AsyncMock(side_effect=_delete)
            return partial

        adapter.send = tracked_send
        channel.get_partial_message = get_partial_message
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=1,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        long_text = ("paragraph " * 250).strip()
        assert len(long_text) > 1900

        task = __import__("asyncio").create_task(consumer.run())
        for offset in range(0, len(long_text), 200):
            consumer.on_delta(long_text[offset : offset + 200])
            await __import__("asyncio").sleep(0.005)
        await __import__("asyncio").sleep(0.1)
        assert consumer._turn_split_delivery is True
        failing_head_id = sent_messages[0]["id"]
        consumer.finish(long_text)
        await task

        assert consumer.final_response_sent is True
        assert consumer.final_content_delivered is True
        assert len(notify_send_attempts) == 1
