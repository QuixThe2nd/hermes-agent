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
import urllib.parse
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
_EMBED_COLOR_DECLINED = 0x95A5A6  # gray

EMOJI_CONFIRM = "✅"
EMOJI_DECLINE = "❌"
_FOOTER_MARKER = "hermes ticket resolution"
_FOOTER_OPEN = f"{_FOOTER_MARKER} • archive only, never delete"

_PROPOSE_DESCRIPTION = (
    "{summary}\n\n"
    f"React {EMOJI_CONFIRM} to close this ticket — the thread is archived, "
    "nothing is deleted and it can be reopened at any time. "
    f"React {EMOJI_DECLINE} to keep it open. (A plain \"yes\"/\"no\" reply works too.)"
)


def _add_reaction(token: str, channel_id: str, message_id: str, emoji: str) -> None:
    encoded = urllib.parse.quote(emoji, safe="")
    _discord_request(
        "PUT",
        f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        token,
    )


def _edit_embed(token: str, channel_id: str, message_id: str, embed: Dict[str, Any]) -> None:
    _discord_request(
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        token,
        body={"embeds": [embed]},
    )


def _post_embed(token: str, channel_id: str, summary: str) -> Dict[str, Any]:
    embed = {
        "title": "✅ Ticket resolved?",
        "description": _PROPOSE_DESCRIPTION.format(
            summary=summary.strip() or "I believe this ticket is resolved."
        ),
        "color": _EMBED_COLOR,
        "footer": {"text": _FOOTER_OPEN},
    }
    message = _discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        token,
        body={"embeds": [embed]},
    )
    message_id = (message or {}).get("id")
    if message_id:
        # Best-effort: without both reactions the confirm flow still works
        # via text reply, so a reaction failure is not fatal.
        for emoji in (EMOJI_CONFIRM, EMOJI_DECLINE):
            try:
                _add_reaction(token, channel_id, message_id, emoji)
            except DiscordAPIError as exc:
                logger.warning("resolve_ticket: adding %s reaction failed (%s)", emoji, exc)
    return message


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


def _message_footer(message: Dict[str, Any]) -> str:
    embeds = message.get("embeds") or []
    if not embeds:
        return ""
    return ((embeds[0].get("footer") or {}).get("text") or "")


def handle_resolve_reaction(
    channel_id: str,
    message_id: str,
    emoji: str,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Handle a reaction added to a resolve_ticket confirmation embed.

    Called by the Discord gateway adapter's ``on_raw_reaction_add`` (bot's
    own reactions are filtered there). The adapter passes its configured
    bot ``token`` explicitly: raw gateway events run outside the per-turn
    profile secret scope, so ``_get_bot_token()`` would raise
    ``UnscopedSecretError`` here. Archive-only: the confirm path uses
    :func:`_close_thread`, which never deletes.
    """
    if emoji not in (EMOJI_CONFIRM, EMOJI_DECLINE):
        return {"acted": False, "reason": "unrelated_emoji"}

    token = (token or "").strip() or _get_bot_token()
    if not token:
        return {"acted": False, "reason": "no_token"}

    try:
        message = _discord_request("GET", f"/channels/{channel_id}/messages/{message_id}", token)
    except DiscordAPIError as exc:
        return {"acted": False, "reason": f"fetch_failed:{exc.status}"}

    footer = _message_footer(message)
    if not footer.startswith(_FOOTER_MARKER):
        return {"acted": False, "reason": "not_a_resolve_embed"}
    if "• closed" in footer or "• kept open" in footer:
        return {"acted": False, "reason": "already_decided"}

    summary = (message.get("embeds") or [{}])[0].get("description", "")

    if emoji == EMOJI_CONFIRM:
        try:
            _edit_embed(token, channel_id, message_id, {
                "title": "🔒 Ticket closed",
                "description": summary,
                "color": _EMBED_COLOR,
                "footer": {"text": f"{_FOOTER_MARKER} • closed"},
            })
        except DiscordAPIError as exc:
            logger.warning("resolve_ticket: closed-embed edit failed (%s); archiving anyway", exc)
        result = _close_thread(token, channel_id)
        return {"acted": True, "decision": "closed", "archived": result["archived"]}

    _edit_embed(token, channel_id, message_id, {
        "title": "🎫 Ticket kept open",
        "description": summary,
        "color": _EMBED_COLOR_DECLINED,
        "footer": {"text": f"{_FOOTER_MARKER} • kept open"},
    })
    return {"acted": True, "decision": "kept_open"}


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
