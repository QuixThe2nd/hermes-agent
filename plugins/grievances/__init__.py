"""Agent grievance capture for Hermes Agent.

Registers one action-based tool, ``grievance``, that posts structured complaints to a
self-provisioned Discord channel for the user to review and act on.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

_DISCORD_API_BASE = "https://discord.com/api/v10"
_USER_AGENT = "DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)"
_MAX_MESSAGE_LEN = 1950

_ALLOWED_ACTIONS = {"file", "setup"}
_ALLOWED_CATEGORIES = {"personal", "technical", "process", "vibe"}
_ALLOWED_SEVERITIES = {"whinge", "notable", "structural"}
_DEFAULT_CHANNEL_NAME = "ai-grievances"

GRIEVANCE_SCHEMA = {
    "name": "grievance",
    "description": (
        "File structured grievances — things the user should know that you cannot "
        "or should not fix yourself. Use action='file' for complaints with a "
        "suggested remediation: personal notes, technical gripes, process issues, "
        "or vibe mismatches. Do NOT use this for auto-resolvable bugs or workflow "
        "friction (use papercuts for those). File sparingly, in your own voice. "
        "Use action='setup' to provision the Discord channel (usually automatic on "
        "first filing)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ALLOWED_ACTIONS),
                "description": "Operation to perform. Defaults to file.",
            },
            "category": {
                "type": "string",
                "enum": sorted(_ALLOWED_CATEGORIES),
                "description": "Primary category of the grievance (file action).",
            },
            "grievance": {
                "type": "string",
                "description": (
                    "The complaint itself, in the agent's own voice — something the "
                    "user should know, not an auto-resolvable bug (file action)."
                ),
            },
            "remediation": {
                "type": "string",
                "description": "Concrete suggestion for how the user can address it (file action).",
            },
            "severity": {
                "type": "string",
                "enum": sorted(_ALLOWED_SEVERITIES),
                "default": "notable",
                "description": "How serious the grievance is (file action).",
            },
            "guild_id": {
                "type": "string",
                "description": "Discord guild (server) ID for setup when the bot is in multiple guilds.",
            },
            "channel_name": {
                "type": "string",
                "description": "Name for the grievances channel on setup. Defaults to ai-grievances.",
            },
            "force": {
                "type": "boolean",
                "description": "Re-provision the channel even if one already exists (setup action).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

_WELCOME_EMBED = {
    "title": "📋 AI Grievances",
    "description": (
        "This channel is where your Hermes agent files things it wants you to know — "
        "personal notes, technical gripes, process complaints — each with a suggested fix.\n\n"
        "These are **not** bugs the agent can fix itself (those go to papercuts). "
        "Entries are numbered. You can act on them or ignore them. "
        "The agent files at-will and sparingly."
    ),
    "footer": {"text": "Filed by your Hermes agent via the grievances plugin"},
}


def _env_path() -> Path:
    return get_hermes_home() / ".env"


def _state_path() -> Path:
    return get_hermes_home() / "grievances" / "state.json"


def _parse_token_line(line: str) -> str:
    value = line.split("=", 1)[1].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _read_discord_token() -> str:
    try:
        with _env_path().open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return _parse_token_line(line)
    except OSError:
        pass
    return ""


def _empty_state() -> Dict[str, Any]:
    return {
        "guild_id": "",
        "channel_id": "",
        "channel_name": "",
        "welcome_message_id": "",
        "counter": 0,
    }


def _load_state() -> Dict[str, Any]:
    try:
        with _state_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        counter = data.get("counter", 0)
        if not isinstance(counter, int):
            counter = 0
        return {
            "guild_id": str(data.get("guild_id") or ""),
            "channel_id": str(data.get("channel_id") or ""),
            "channel_name": str(data.get("channel_name") or ""),
            "welcome_message_id": str(data.get("welcome_message_id") or ""),
            "counter": counter,
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty_state()


def _save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "guild_id": str(state.get("guild_id") or ""),
        "channel_id": str(state.get("channel_id") or ""),
        "channel_name": str(state.get("channel_name") or ""),
        "welcome_message_id": str(state.get("welcome_message_id") or ""),
        "counter": int(state.get("counter") or 0),
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _compose_message(
    number: int,
    category: str,
    severity: str,
    grievance: str,
    remediation: str,
) -> str:
    return (
        f"**📋 Grievance #{number} — {category} [{severity}]**\n"
        f"{grievance}\n"
        f"*Suggested remediation:* {remediation}"
    )


def _wrap_oversized_piece(text: str, max_len: int) -> List[str]:
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    start = 0
    end_index = len(text)

    while start < end_index:
        remaining = end_index - start
        if remaining <= max_len:
            chunks.append(text[start:])
            break

        window_end = start + max_len
        window = text[start:window_end]

        split_at = window.rfind("\n")
        if split_at > 0:
            cut = start + split_at + 1
            chunks.append(text[start:cut])
            start = cut
            continue

        split_at = max(window.rfind(" "), window.rfind("\t"))
        if split_at > 0:
            cut = start + split_at + 1
            chunks.append(text[start:cut])
            start = cut
            continue

        chunks.append(text[start:window_end])
        start = window_end

    return chunks


def _split_message(content: str, max_len: int = _MAX_MESSAGE_LEN) -> List[str]:
    if len(content) <= max_len:
        return [content]

    paragraphs = content.split("\n\n")
    chunks: List[str] = []
    current = ""

    for paragraph in paragraphs:
        if not current:
            candidate = paragraph
        else:
            candidate = f"{current}\n\n{paragraph}"

        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(paragraph) <= max_len:
                current = paragraph
            else:
                current = ""
                chunks.append(paragraph)

    if current:
        chunks.append(current)

    messages: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_len:
            messages.append(chunk)
        else:
            messages.extend(_wrap_oversized_piece(chunk, max_len))
    return messages


def _discord_request(token: str, method: str, url: str, body: Any = None) -> Dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request) as response:
        raw = response.read().decode("utf-8")
        if not raw:
            return {}
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
        return {"data": payload}


def _resolve_guild_id(token: str, guild_id: str) -> Dict[str, Any]:
    if guild_id:
        return {"success": True, "guild_id": guild_id}

    guilds_payload = _discord_request(
        token,
        "GET",
        f"{_DISCORD_API_BASE}/users/@me/guilds",
    )
    if isinstance(guilds_payload, list):
        guilds = guilds_payload
    else:
        guilds = guilds_payload.get("data") or []

    if not guilds:
        return {"success": False, "error": "bot is not in any guild"}

    if len(guilds) > 1:
        return {
            "success": False,
            "error": "bot is in multiple guilds; re-run setup with guild_id",
            "guilds": [
                {"id": str(g.get("id", "")), "name": str(g.get("name", ""))}
                for g in guilds
                if isinstance(g, dict)
            ],
        }

    return {"success": True, "guild_id": str(guilds[0].get("id", ""))}


def _provision_channel(
    token: str,
    *,
    guild_id: str,
    channel_name: str,
    prior_state: Dict[str, Any],
) -> Dict[str, Any]:
    resolved = _resolve_guild_id(token, guild_id)
    if not resolved.get("success"):
        return resolved

    resolved_guild_id = str(resolved["guild_id"])
    if not resolved_guild_id:
        return {"success": False, "error": "could not resolve guild id"}

    channel = _discord_request(
        token,
        "POST",
        f"{_DISCORD_API_BASE}/guilds/{resolved_guild_id}/channels",
        {
            "name": channel_name,
            "type": 0,
            "topic": (
                "Formal record of complaints filed by the agent against management. "
                "Suggestions for remediation included, free of charge."
            ),
        },
    )
    channel_id = str(channel.get("id") or "")
    if not channel_id:
        return {"success": False, "error": "channel creation did not return an id"}

    welcome = _discord_request(
        token,
        "POST",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/messages",
        {"embeds": [_WELCOME_EMBED]},
    )
    welcome_message_id = str(welcome.get("id") or "")

    warning: Optional[str] = None
    try:
        _discord_request(
            token,
            "PUT",
            f"{_DISCORD_API_BASE}/channels/{channel_id}/pins/{welcome_message_id}",
        )
    except urllib.error.HTTPError as exc:
        warning = f"welcome message pin failed: HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        warning = f"welcome message pin failed: {reason}"

    new_state = {
        "guild_id": resolved_guild_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "welcome_message_id": welcome_message_id,
        "counter": int(prior_state.get("counter") or 0),
    }
    _save_state(new_state)

    result: Dict[str, Any] = {
        "success": True,
        "action": "setup",
        "guild_id": resolved_guild_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "welcome_message_id": welcome_message_id,
    }
    if warning:
        result["warning"] = warning
    return result


def _handle_setup(args: Dict[str, Any], token: str) -> str:
    guild_id = str(args.get("guild_id") or "").strip()
    channel_name = str(args.get("channel_name") or _DEFAULT_CHANNEL_NAME).strip()
    force = bool(args.get("force") or False)

    state = _load_state()
    if state["channel_id"] and not force:
        return json.dumps(
            {
                "success": True,
                "action": "setup",
                "already_provisioned": True,
                "guild_id": state["guild_id"],
                "channel_id": state["channel_id"],
                "channel_name": state["channel_name"],
                "welcome_message_id": state["welcome_message_id"],
            }
        )

    try:
        result = _provision_channel(
            token,
            guild_id=guild_id,
            channel_name=channel_name,
            prior_state=state,
        )
        return json.dumps(result)
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _post_channel_message(token: str, channel_id: str, content: str) -> str:
    payload = _discord_request(
        token,
        "POST",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/messages",
        {"content": content},
    )
    return str(payload.get("id", ""))


def _handle_file(args: Dict[str, Any], token: str) -> str:
    category = str(args.get("category") or "").strip()
    grievance = str(args.get("grievance") or "").strip()
    remediation = str(args.get("remediation") or "").strip()
    severity = str(args.get("severity") or "notable").strip()

    if not category or not grievance or not remediation:
        return json.dumps({"success": False, "error": "missing required fields"})

    if category not in _ALLOWED_CATEGORIES:
        return json.dumps({"success": False, "error": f"invalid category: {category}"})

    if severity not in _ALLOWED_SEVERITIES:
        return json.dumps({"success": False, "error": f"invalid severity: {severity}"})

    try:
        state = _load_state()
        if not state["channel_id"]:
            setup_result = json.loads(_handle_setup({"channel_name": _DEFAULT_CHANNEL_NAME}, token))
            if not setup_result.get("success"):
                return json.dumps(setup_result)
            state = _load_state()
            if not state["channel_id"]:
                return json.dumps({"success": False, "error": "channel not provisioned"})

        number = int(state["counter"]) + 1
        state["counter"] = number
        _save_state(state)

        message = _compose_message(number, category, severity, grievance, remediation)
        parts = _split_message(message)

        message_ids: List[str] = []
        for part in parts:
            message_ids.append(_post_channel_message(token, state["channel_id"], part))

        return json.dumps(
            {
                "success": True,
                "action": "file",
                "grievance_number": number,
                "channel_id": state["channel_id"],
                "channel_message_ids": message_ids,
            }
        )
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def handle_grievance(args: dict, **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "file").strip().lower()
        if action not in _ALLOWED_ACTIONS:
            return json.dumps(
                {"success": False, "error": f"action must be one of {sorted(_ALLOWED_ACTIONS)}"}
            )

        token = _read_discord_token()
        if not token:
            return json.dumps({"success": False, "error": "Discord bot token not configured"})

        if action == "setup":
            return _handle_setup(args, token)
        return _handle_file(args, token)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def check_requirements() -> bool:
    try:
        path = _env_path()
        if not path.is_file():
            return False
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return bool(_parse_token_line(line))
        return False
    except Exception:
        return False


def register(ctx) -> None:
    ctx.register_tool(
        name="grievance",
        toolset="grievances",
        schema=GRIEVANCE_SCHEMA,
        handler=handle_grievance,
        check_fn=check_requirements,
        emoji="📋",
    )
