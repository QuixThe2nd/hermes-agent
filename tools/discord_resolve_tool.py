"""Ticket-resolution tool for Discord threads.

Gives the model an explicit "this conversation is resolved" signal:
``action='propose'`` posts an embed in the thread asking the user to
confirm closing the ticket; once the user confirms, ``action='close'``
archives the thread.

Safety contract: closing a ticket ONLY ever archives the thread
(PATCH {"archived": true}). This module contains no DELETE calls and
refuses to act on non-thread channels, so ticket history can never be
destroyed by this tool. Archiving is fully reversible (anyone can
unarchive / send a message to reopen).

Registered in the ``discord`` toolset, so it costs nothing on other
platforms. Reuses the REST helper and token resolution from
``tools.discord_tool`` — no duplicated plumbing.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from tools.discord_tool import (
    DiscordAPIError,
    _discord_request,
    _get_bot_token,
    check_discord_tool_requirements,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_THREAD_TYPES = {10, 11, 12}  # announcement_thread, public_thread, private_thread
_EMBED_COLOR = 0x57F287  # Discord green

_PROPOSE_DESCRIPTION = (
    "{summary}\n\n"
    "Reply **yes** (or \"close it\") to close this ticket — the thread is "
    "archived, nothing is deleted and it can be reopened at any time. "
    "Reply **no** to keep it open."
)


def _post_embed(token: str, channel_id: str, summary: str) -> Dict[str, Any]:
    embed = {
        "title": "✅ Ticket resolved?",
        "description": _PROPOSE_DESCRIPTION.format(
            summary=summary.strip() or "I believe this ticket is resolved."
        ),
        "color": _EMBED_COLOR,
        "footer": {"text": "hermes ticket resolution • archive only, never delete"},
    }
    return _discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        token,
        body={"embeds": [embed]},
    )


def _close_thread(token: str, channel_id: str) -> Dict[str, Any]:
    """Archive a thread. Never deletes. Idempotent."""
    channel = _discord_request("GET", f"/channels/{channel_id}", token)
    channel_type = channel.get("type")
    if channel_type not in _THREAD_TYPES:
        raise DiscordAPIError(
            400,
            f"Channel {channel_id} is not a thread (type={channel_type}); "
            "refusing to close. resolve_ticket only closes threads.",
        )

    thread_meta = channel.get("thread_metadata") or {}
    if thread_meta.get("archived"):
        return {"archived": True, "already_archived": True}

    # Best-effort farewell message before archiving. If the bot can't post
    # (permissions), archiving still proceeds.
    try:
        _discord_request(
            "POST",
            f"/channels/{channel_id}/messages",
            token,
            body={"content": "🔒 Ticket closed — this thread has been archived. Nothing was deleted; ask here to reopen it."},
        )
    except DiscordAPIError as exc:
        logger.warning("resolve_ticket: farewell message failed (%s); archiving anyway", exc)

    _discord_request("PATCH", f"/channels/{channel_id}", token, body={"archived": True})

    # Verify the final state independently.
    after = _discord_request("GET", f"/channels/{channel_id}", token)
    archived = bool((after.get("thread_metadata") or {}).get("archived"))
    return {"archived": archived, "already_archived": False}


def resolve_ticket(
    action: str = "propose",
    channel_id: str = "",
    summary: str = "",
    task_id: Optional[str] = None,
) -> str:
    token = _get_bot_token()
    if not token:
        return tool_error("DISCORD_BOT_TOKEN is not configured.")

    channel_id = (channel_id or "").strip()
    if not channel_id:
        return tool_error("channel_id is required — pass the current thread ID from session context.")

    try:
        if action == "propose":
            message = _post_embed(token, channel_id, summary)
            return json.dumps({
                "success": True,
                "action": "propose",
                "channel_id": channel_id,
                "message_id": (message or {}).get("id"),
                "note": "Confirmation embed posted. Call action='close' only after the user explicitly confirms.",
            })
        if action == "close":
            result = _close_thread(token, channel_id)
            return json.dumps({
                "success": bool(result["archived"]),
                "action": "close",
                "channel_id": channel_id,
                "archived": result["archived"],
                "already_archived": result["already_archived"],
                "deleted": False,
                "note": "Thread archived (reversible). Nothing was deleted.",
            })
        return tool_error(f"Unknown action '{action}'. Use 'propose' or 'close'.")
    except DiscordAPIError as exc:
        return tool_error(f"Discord API error {exc.status}: {exc.body[:500]}")


_SCHEMA: Dict[str, Any] = {
    "name": "resolve_ticket",
    "description": (
        "Use when you believe the conversation in this Discord thread is resolved. "
        "action='propose' posts an embed asking the user to confirm closing the ticket. "
        "Only use action='close' AFTER the user explicitly confirms (e.g. 'yes', 'close it', 'resolve'). "
        "Closing ARCHIVES the thread (fully reversible) — it NEVER deletes the thread or its messages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["propose", "close"],
                "description": "'propose' asks the user to confirm; 'close' archives the thread after confirmation.",
                "default": "propose",
            },
            "channel_id": {
                "type": "string",
                "description": "The current Discord thread/channel ID from session context.",
            },
            "summary": {
                "type": "string",
                "description": "One-line resolution summary shown in the confirmation embed (propose only).",
            },
        },
        "required": ["action", "channel_id"],
    },
}

registry.register(
    name="resolve_ticket",
    toolset="discord",
    schema=_SCHEMA,
    handler=lambda args, **kw: resolve_ticket(
        action=args.get("action", "propose"),
        channel_id=args.get("channel_id", ""),
        summary=args.get("summary", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_discord_tool_requirements,
    requires_env=["DISCORD_BOT_TOKEN"],
)
