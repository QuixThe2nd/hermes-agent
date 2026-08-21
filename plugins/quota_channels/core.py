"""Core quota-channels logic — provider fetches, Discord updates, tick orchestration."""

from __future__ import annotations

import json
import math
import os
import struct
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

HttpFn = Callable[[urllib.request.Request, float], Tuple[int, bytes]]
SleepFn = Callable[[float], None]
NowFn = Callable[[], float]

PROVIDER_SPECS: Tuple[Tuple[str, str], ...] = (
    ("codex", "Codex"),
    ("kimi", "Kimi"),
    ("zai", "z.ai"),
    ("cursor", "Cursor"),
    ("grok", "Grok"),
)

DEFAULT_QUOTA_INTERVAL_SECONDS = 1800
DEFAULT_POST_QUOTA_DELAY_SECONDS = 31

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
ZAI_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
CURSOR_USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
GROK_USAGE_URL = (
    "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
)
TOKEN_URL = "https://auth.openai.com/oauth/token"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

STATE_FILENAME = "quota_channels_state.json"


class QuotaChannelsError(Exception):
    """Raised instead of sys.exit from the reference script."""


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def state_path() -> Path:
    return _hermes_home() / STATE_FILENAME


def _read_env_key(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as exc:
        raise QuotaChannelsError(f"cannot read {path}: {exc}") from exc
    raise QuotaChannelsError(f"{key} missing in {path}")


def discord_token() -> str:
    return _read_env_key(_hermes_home() / "secrets" / "discord.env", "DISCORD_BOT_TOKEN")


def kimi_api_key() -> str:
    return _read_env_key(_hermes_home() / ".env", "KIMI_API_KEY")


def zai_api_key() -> str:
    return _read_env_key(_hermes_home() / "secrets" / "zai.env", "ZAI_API_KEY")


def cursor_access_token() -> str:
    cursor_auth = Path.home() / ".config" / "cursor" / "auth.json"
    try:
        token = json.loads(cursor_auth.read_text(encoding="utf-8")).get("accessToken")
    except (OSError, json.JSONDecodeError) as exc:
        raise QuotaChannelsError(f"cannot read {cursor_auth}: {exc}") from exc
    if not token:
        raise QuotaChannelsError(f"no accessToken in {cursor_auth}")
    return token


def load_store() -> dict:
    auth_path = _hermes_home() / "auth.json"
    try:
        return json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuotaChannelsError(f"cannot read {auth_path}: {exc}") from exc


def save_store(store: dict) -> None:
    auth_path = _hermes_home() / "auth.json"
    fd, tmp = tempfile.mkstemp(
        dir=str(auth_path.parent), prefix=".auth.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle, indent=2)
        os.replace(tmp, auth_path)
    except OSError as exc:
        raise QuotaChannelsError(f"cannot write {auth_path}: {exc}") from exc


def load_state() -> dict:
    path = state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".quota-state.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        raise QuotaChannelsError(f"cannot write {path}: {exc}") from exc


def save_state(now_fn: NowFn = time.time) -> int:
    state = load_state()
    if not isinstance(state, dict):
        state = {}
    state["last_quota_success"] = int(now_fn())
    _write_state(state)
    return state["last_quota_success"]


def default_http(req: urllib.request.Request, timeout: float = 25.0) -> Tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise QuotaChannelsError(
            f"network error: {type(exc).__name__}: {exc}"
        ) from exc


def http_text(
    req: urllib.request.Request,
    http_fn: HttpFn = default_http,
    timeout: float = 25.0,
) -> Tuple[int, str]:
    status, body = http_fn(req, timeout)
    if isinstance(body, bytes):
        return status, body.decode(errors="replace")
    return status, body


def http_bin(
    req: urllib.request.Request,
    http_fn: HttpFn = default_http,
    timeout: float = 25.0,
) -> Tuple[int, bytes]:
    status, body = http_fn(req, timeout)
    if isinstance(body, str):
        return status, body.encode()
    return status, body


def parse_codex_usage(text: str) -> Tuple[int, float]:
    try:
        usage = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError("codex: invalid usage payload JSON") from exc
    if not isinstance(usage, dict):
        raise QuotaChannelsError("codex: invalid usage payload JSON")
    primary = (usage.get("rate_limit") or {}).get("primary_window")
    if not primary:
        raise QuotaChannelsError(
            f"no primary_window in codex usage payload: {text[:200]}"
        )
    if not isinstance(primary, dict):
        raise QuotaChannelsError("codex: invalid primary_window in usage payload")
    try:
        used = round(float(primary.get("used_percent", 0)))
        reset_after = float(primary.get("reset_after_seconds", 0))
    except (TypeError, ValueError) as exc:
        raise QuotaChannelsError(
            "codex: invalid primary_window fields in usage payload"
        ) from exc
    remaining = max(0, 100 - used)
    reset_secs = max(0.0, reset_after)
    return remaining, reset_secs


def format_reset_left(seconds: float) -> str:
    # granular reset countdown: days at 2+ days out, then hours, then minutes
    secs = max(0, seconds)
    if secs >= 172800:
        return f"{math.ceil(secs / 86400)}d left"
    if secs >= 3600:
        return f"{math.ceil(secs / 3600)}h left"
    return f"{max(1, math.ceil(secs / 60))}m left"


def format_codex_name(remaining: int, reset_secs: float) -> str:
    return f"Codex: {remaining}% \u2022 {format_reset_left(reset_secs)}"


def parse_kimi_usage(text: str, now_fn: NowFn = time.time) -> Tuple[int, float]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError("kimi: invalid usage payload JSON") from exc
    if not isinstance(payload, dict):
        raise QuotaChannelsError("kimi: invalid usage payload JSON")
    usage = payload.get("usage")
    if not usage:
        raise QuotaChannelsError(f"no usage object in kimi payload: {text[:200]}")
    if not isinstance(usage, dict):
        raise QuotaChannelsError("kimi: invalid usage object in payload")
    try:
        remaining = int(usage["remaining"])
    except (KeyError, TypeError, ValueError) as exc:
        raise QuotaChannelsError("kimi: invalid remaining in usage payload") from exc
    reset_raw = usage.get("resetTime")
    if not isinstance(reset_raw, str):
        raise QuotaChannelsError("kimi: invalid resetTime in usage payload")
    try:
        if reset_raw.endswith("Z"):
            reset_raw = reset_raw[:-1] + "+00:00"
        reset_at = datetime.fromisoformat(reset_raw)
    except ValueError as exc:
        raise QuotaChannelsError("kimi: invalid resetTime in usage payload") from exc
    if reset_at.tzinfo is None or reset_at.utcoffset() is None:
        raise QuotaChannelsError("kimi: invalid resetTime in usage payload")
    now = datetime.fromtimestamp(now_fn(), tz=timezone.utc)
    try:
        reset_secs = max(0.0, (reset_at - now).total_seconds())
    except TypeError as exc:
        raise QuotaChannelsError("kimi: invalid resetTime in usage payload") from exc
    return remaining, reset_secs


def format_kimi_name(remaining: int, reset_secs: float) -> str:
    return f"Kimi: {remaining}% \u2022 {format_reset_left(reset_secs)}"


def parse_zai_usage(text: str, now_fn: NowFn = time.time) -> Tuple[int, float]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError("z.ai: invalid usage payload JSON") from exc
    if not isinstance(payload, dict):
        raise QuotaChannelsError("z.ai: invalid usage payload JSON")
    data = payload.get("data")
    if data is None:
        limits = []
    elif not isinstance(data, Mapping):
        raise QuotaChannelsError("z.ai: invalid limits fields in usage payload")
    else:
        limits = data.get("limits") or []
    if not limits:
        raise QuotaChannelsError(f"no limits in z.ai payload: {text[:200]}")
    for entry in limits:
        if not isinstance(entry, Mapping):
            raise QuotaChannelsError("z.ai: invalid limits fields in usage payload")
    try:
        weekly = max(limits, key=lambda window: window.get("nextResetTime") or 0)
        used = int(weekly.get("percentage", 0))
        reset_ms = float(weekly.get("nextResetTime") or 0)
    except (AttributeError, TypeError, ValueError) as exc:
        raise QuotaChannelsError("z.ai: invalid limits fields in usage payload") from exc
    remaining = max(0, 100 - used)
    reset_secs = max(0.0, reset_ms / 1000 - now_fn())
    return remaining, reset_secs


def format_zai_name(remaining: int, reset_secs: float) -> str:
    return f"z.ai: {remaining}% \u2022 {format_reset_left(reset_secs)}"


def parse_cursor_usage(
    text: str, now_fn: NowFn = time.time
) -> Tuple[int, int, float]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError("cursor: invalid usage payload JSON") from exc
    if not isinstance(payload, dict):
        raise QuotaChannelsError("cursor: invalid usage payload JSON")
    plan = payload.get("planUsage")
    if not plan:
        raise QuotaChannelsError(f"no planUsage in cursor payload: {text[:200]}")
    if not isinstance(plan, dict):
        raise QuotaChannelsError("cursor: invalid planUsage in usage payload")
    try:
        cursor_models = max(0, 100 - math.floor(float(plan.get("autoPercentUsed") or 0)))
        other_models = max(0, 100 - math.floor(float(plan.get("apiPercentUsed") or 0)))
        end_ms = float(payload.get("billingCycleEnd") or 0)
    except (TypeError, ValueError) as exc:
        raise QuotaChannelsError("cursor: invalid planUsage fields in usage payload") from exc
    reset_secs = max(0.0, end_ms / 1000 - now_fn())
    return cursor_models, other_models, reset_secs


def format_cursor_name(
    auto_remaining: int, api_remaining: int, reset_secs: float
) -> str:
    return (
        f"Cursor: {auto_remaining}%/{api_remaining}% \u2022 {format_reset_left(reset_secs)}"
    )


def pb_varint(buf: bytes, i: int) -> Tuple[int, int]:
    val = shift = 0
    nbytes = 0
    while True:
        if i >= len(buf):
            raise QuotaChannelsError("grok: truncated protobuf varint")
        b = buf[i]
        i += 1
        nbytes += 1
        if nbytes > 10 or shift > 63:
            raise QuotaChannelsError("grok: overlong protobuf varint")
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def pb_fields(buf: bytes):
    i = 0
    while i < len(buf):
        key, i = pb_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = pb_varint(buf, i)
        elif wire == 1:
            if i + 8 > len(buf):
                raise QuotaChannelsError("grok: truncated protobuf field")
            val = int.from_bytes(buf[i : i + 8], "little")
            i += 8
        elif wire == 2:
            n, i = pb_varint(buf, i)
            if i + n > len(buf):
                raise QuotaChannelsError("grok: truncated protobuf field")
            val = buf[i : i + n]
            i += n
        elif wire == 5:
            if i + 4 > len(buf):
                raise QuotaChannelsError("grok: truncated protobuf field")
            val = int.from_bytes(buf[i : i + 4], "little")
            i += 4
        else:
            raise QuotaChannelsError(f"grok: unsupported protobuf wire type {wire}")
        yield field, wire, val


def grpc_web_unwrap(body: bytes) -> bytes:
    msg = b""
    i = 0
    while i + 5 <= len(body):
        flag = body[i]
        try:
            n = int.from_bytes(body[i + 1 : i + 5], "big")
        except struct.error as exc:
            raise QuotaChannelsError(
                "grok: truncated gRPC-web frame in billing response"
            ) from exc
        frame = body[i + 5 : i + 5 + n]
        if len(frame) != n:
            raise QuotaChannelsError("grok: truncated gRPC-web frame in billing response")
        if flag == 0:
            msg += frame
        i += 5 + n
    trailing = len(body) - i
    if trailing:
        raise QuotaChannelsError("grok: truncated gRPC-web frame in billing response")
    return msg


def parse_grok_usage(
    body_bytes: bytes, now_fn: NowFn = time.time
) -> Tuple[int, float]:
    try:
        config = None
        for field, wire, val in pb_fields(grpc_web_unwrap(body_bytes)):
            if field == 1 and wire == 2:
                config = val
        if config is None:
            raise QuotaChannelsError("grok: no config message in billing response")

        ratio_present = False
        used_pct = 0.0
        period_end = 0
        usage_period_type = None
        for field, wire, val in pb_fields(config):
            if field == 1 and wire == 5:
                ratio_present = True
                used_pct = struct.unpack("<f", val.to_bytes(4, "little"))[0]
            elif field == 5 and wire == 2:
                for tfield, twire, tval in pb_fields(val):
                    if tfield == 1 and twire == 0:
                        period_end = tval
            elif field == 8 and wire == 2:
                for sfield, swire, sval in pb_fields(val):
                    if sfield == 1 and swire == 0:
                        usage_period_type = sval
        reset_secs = max(0.0, period_end - now_fn())
        if ratio_present:
            remaining = round(100 - used_pct)
            return remaining, reset_secs
        if usage_period_type in (1, 2) and period_end > 0:
            return 100, reset_secs
        raise QuotaChannelsError(
            "grok: no usage percentage or reset timestamp in billing config"
        )
    except QuotaChannelsError:
        raise
    except (IndexError, struct.error, ValueError, TypeError, KeyError) as exc:
        raise QuotaChannelsError("grok: invalid billing response protobuf") from exc


def format_grok_name(remaining: int, reset_secs: float) -> str:
    return f"Grok: {remaining}% \u2022 {format_reset_left(reset_secs)}"


def _fmt_clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{suffix}"


def fmt_ts(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch)
    return f"{dt.day}/{dt.month} {_fmt_clock(dt)}"


def fmt_time(epoch: float) -> str:
    return _fmt_clock(datetime.fromtimestamp(epoch))


def category_name(
    last_success: float,
    interval: int,
    now_fn: NowFn = time.time,
) -> str:
    if last_success <= 0:
        return "Quotas \u2022 never \u2022 Next: Due"
    now = now_fn()
    next_due = last_success + interval
    ts_part = fmt_ts(last_success)
    if now >= next_due:
        return f"Quotas \u2022 {ts_part} \u2022 Next: Due"
    return f"Quotas \u2022 {ts_part} \u2022 Next: {fmt_time(next_due)}"


TS_CHANNEL_PENDING_NAME = "pending"
TS_CHANNELS_KEY = "ts_channels"


def parse_last_quota_success(value: Any) -> Optional[float]:
    """Finite positive epoch from persisted state, or None if invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    try:
        datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None
    return ts


def timestamp_channel_name(last_success: Any) -> str:
    # name derives only from the persisted last-success timestamp; tick time
    # must never leak in here
    ts = parse_last_quota_success(last_success)
    if ts is None:
        return TS_CHANNEL_PENDING_NAME
    return fmt_ts(ts)


def normalize_enabled_providers(raw: Any) -> Dict[str, bool]:
    if raw is None:
        return {key: True for key, _ in PROVIDER_SPECS}
    if isinstance(raw, list):
        enabled = {key: False for key, _ in PROVIDER_SPECS}
        for item in raw:
            if not isinstance(item, str):
                continue
            enabled[item.strip().lower()] = True
        return enabled
    if isinstance(raw, dict):
        enabled = {key: False for key, _ in PROVIDER_SPECS}
        for name, value in raw.items():
            enabled[str(name).strip().lower()] = bool(value)
        return enabled
    raise QuotaChannelsError(
        "quota_channels.enabled_providers must be a mapping or list"
    )


def validate_quota_config(section: Mapping[str, Any]) -> dict:
    if not isinstance(section, Mapping):
        raise QuotaChannelsError("quota_channels config must be a mapping")

    guild_id = section.get("guild_id")
    category_id = section.get("category_id")
    if not guild_id or not category_id:
        raise QuotaChannelsError(
            "quota_channels requires guild_id and category_id in config.yaml"
        )

    channel_ids = section.get("channel_ids") or {}
    if not isinstance(channel_ids, Mapping):
        raise QuotaChannelsError("quota_channels.channel_ids must be a mapping")

    enabled = normalize_enabled_providers(section.get("enabled_providers"))
    active: List[Tuple[str, str, str]] = []
    for key, label in PROVIDER_SPECS:
        if not enabled.get(key, False):
            continue
        channel_id = channel_ids.get(key)
        if not channel_id:
            raise QuotaChannelsError(
                f"quota_channels.channel_ids.{key} required when {key} is enabled"
            )
        active.append((key, label, str(channel_id)))

    if not active:
        raise QuotaChannelsError(
            "quota_channels requires at least one enabled provider with a channel id"
        )

    return {
        "guild_id": str(guild_id),
        "category_id": str(category_id),
        "channel_ids": {key: cid for key, _, cid in active},
        "providers": active,
        "quota_interval_seconds": int(
            section.get("quota_interval_seconds", DEFAULT_QUOTA_INTERVAL_SECONDS)
        ),
        # Deprecated: accepted for backward compatibility but no longer used by run_tick.
        "post_quota_delay_seconds": int(
            section.get("post_quota_delay_seconds", DEFAULT_POST_QUOTA_DELAY_SECONDS)
        ),
    }


def check_minimum_config_from_mapping(config: Mapping[str, Any]) -> bool:
    try:
        section = config.get("quota_channels")
        if not isinstance(section, Mapping):
            return False
        validate_quota_config(section)
        return True
    except QuotaChannelsError:
        return False
    except Exception:
        return False


def load_quota_config(config_path: Optional[Path] = None) -> dict:
    if config_path is None:
        try:
            from hermes_cli.config import load_config_readonly

            raw = load_config_readonly()
        except Exception as exc:
            raise QuotaChannelsError(f"cannot load config: {exc}") from exc
    else:
        import yaml

        try:
            raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise QuotaChannelsError(f"cannot read {config_path}: {exc}") from exc
        except Exception as exc:
            raise QuotaChannelsError(f"cannot parse {config_path}: {exc}") from exc
    section = raw.get("quota_channels")
    if section is None:
        raise QuotaChannelsError("quota_channels section missing in config.yaml")
    return validate_quota_config(section)


def discord_headers() -> dict:
    return {
        "Authorization": "Bot " + discord_token(),
        "User-Agent": "DiscordBot (https://github.com/hermes-agent, 1.0)",
        "Content-Type": "application/json",
    }


def fetch_channel_name(
    channel_id: str,
    headers: dict,
    http_fn: HttpFn = default_http,
) -> str:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}", headers=headers
    )
    status, text = http_text(req, http_fn=http_fn)
    if status != 200:
        raise QuotaChannelsError(
            f"discord channel fetch returned {status}: {text[:200]}"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError("discord: invalid channel response JSON") from exc
    return data.get("name")


def fetch_channel_json(
    channel_id: str,
    headers: dict,
    http_fn: HttpFn = default_http,
) -> Optional[dict]:
    """Full channel object, or None when the channel no longer exists (404)."""
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}", headers=headers
    )
    status, text = http_text(req, http_fn=http_fn)
    if status == 404:
        return None
    if status != 200:
        raise QuotaChannelsError(
            f"discord channel fetch returned {status}: {text[:200]}"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError("discord: invalid channel response JSON") from exc
    return data if isinstance(data, dict) else None


def rename_channel(
    channel_id: str,
    name: str,
    headers: dict,
    *,
    skip_on_429: bool = False,
    http_fn: HttpFn = default_http,
) -> str:
    if fetch_channel_name(channel_id, headers, http_fn=http_fn) == name:
        return "unchanged"
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}",
        data=json.dumps({"name": name}).encode(),
        headers=headers,
        method="PATCH",
    )
    status, text = http_text(req, http_fn=http_fn)
    if status == 429 and skip_on_429:
        return "skipped"
    if status != 200:
        raise QuotaChannelsError(f"discord rename returned {status}: {text[:200]}")
    return "renamed"


def refresh_codex_tokens(
    store: dict,
    http_fn: HttpFn = default_http,
) -> str:
    toks = store["providers"]["openai-codex"]["tokens"]
    body = json.dumps(
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": toks["refresh_token"],
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    status, text = http_text(req, http_fn=http_fn)
    if status != 200:
        raise QuotaChannelsError(f"codex token refresh failed ({status}): {text[:200]}")
    try:
        new = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError("codex token refresh failed: invalid JSON response") from exc
    if not isinstance(new, dict) or "access_token" not in new:
        raise QuotaChannelsError("codex token refresh failed: missing access_token in response")
    toks["access_token"] = new["access_token"]
    if new.get("refresh_token"):
        toks["refresh_token"] = new["refresh_token"]
    if new.get("id_token"):
        toks["id_token"] = new["id_token"]
    save_store(store)
    return toks["access_token"]


def refresh_xai_tokens(
    store: dict,
    http_fn: HttpFn = default_http,
) -> str:
    toks = store["providers"]["xai-oauth"]["tokens"]
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": toks["refresh_token"],
        }
    ).encode()
    req = urllib.request.Request(
        XAI_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    status, text = http_text(req, http_fn=http_fn)
    if status != 200:
        raise QuotaChannelsError(
            f"xai token refresh failed ({status}): xai re-login required to refresh auth"
        )
    try:
        new = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError(
            "xai token refresh failed: invalid JSON response; xai re-login required to refresh auth"
        ) from exc
    if not isinstance(new, dict) or "access_token" not in new:
        raise QuotaChannelsError(
            "xai token refresh failed: missing access_token in response; xai re-login required to refresh auth"
        )
    for key in ("access_token", "refresh_token", "id_token"):
        if new.get(key):
            toks[key] = new[key]
    save_store(store)
    return toks["access_token"]


def fetch_codex_usage(
    access: str,
    http_fn: HttpFn = default_http,
) -> Tuple[int, str]:
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {access}", "User-Agent": "codex-cli"},
    )
    return http_text(req, http_fn=http_fn)


def fetch_kimi_usage(
    api_key: str,
    http_fn: HttpFn = default_http,
) -> Tuple[int, str]:
    req = urllib.request.Request(
        KIMI_USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "hermes-quota-channel",
        },
    )
    return http_text(req, http_fn=http_fn)


def fetch_zai_usage(
    api_key: str,
    http_fn: HttpFn = default_http,
) -> Tuple[int, str]:
    req = urllib.request.Request(
        ZAI_USAGE_URL,
        headers={"Authorization": api_key, "User-Agent": "hermes-quota-channel"},
    )
    return http_text(req, http_fn=http_fn)


def fetch_cursor_usage(
    access: str,
    http_fn: HttpFn = default_http,
) -> Tuple[int, str]:
    req = urllib.request.Request(
        CURSOR_USAGE_URL,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hermes-quota-channel",
        },
        method="POST",
    )
    return http_text(req, http_fn=http_fn)


def fetch_grok_usage(
    access: str,
    http_fn: HttpFn = default_http,
) -> Tuple[int, bytes]:
    req = urllib.request.Request(
        GROK_USAGE_URL,
        data=b"\x00\x00\x00\x00\x00",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/grpc-web+proto",
            "Accept": "application/grpc-web+proto",
            "X-Grpc-Web": "1",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/",
            "User-Agent": "hermes-quota-channel",
        },
        method="POST",
    )
    return http_bin(req, http_fn=http_fn)


def run_codex_provider(
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> Tuple[str, float, str]:
    store = load_store()
    toks = store.get("providers", {}).get("openai-codex", {}).get("tokens", {})
    access = toks.get("access_token")
    if not access:
        raise QuotaChannelsError("no openai-codex access token in hermes auth store")
    status, text = fetch_codex_usage(access, http_fn=http_fn)
    if status == 401:
        access = refresh_codex_tokens(store, http_fn=http_fn)
        status, text = fetch_codex_usage(access, http_fn=http_fn)
    if status != 200:
        raise QuotaChannelsError(
            f"codex usage endpoint returned {status}: {text[:200]}"
        )
    remaining, reset_secs = parse_codex_usage(text)
    return format_codex_name(remaining, reset_secs), reset_secs, "Codex"


def run_kimi_provider(
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> Tuple[str, float, str]:
    status, text = fetch_kimi_usage(kimi_api_key(), http_fn=http_fn)
    if status != 200:
        raise QuotaChannelsError(f"kimi usage endpoint returned {status}: {text[:200]}")
    remaining, reset_secs = parse_kimi_usage(text, now_fn=now_fn)
    return format_kimi_name(remaining, reset_secs), reset_secs, "Kimi"


def run_zai_provider(
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> Tuple[str, float, str]:
    status, text = fetch_zai_usage(zai_api_key(), http_fn=http_fn)
    if status != 200:
        raise QuotaChannelsError(f"z.ai usage endpoint returned {status}: {text[:200]}")
    remaining, reset_secs = parse_zai_usage(text, now_fn=now_fn)
    return format_zai_name(remaining, reset_secs), reset_secs, "z.ai"


def run_cursor_provider(
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> Tuple[str, float, str]:
    status, text = fetch_cursor_usage(cursor_access_token(), http_fn=http_fn)
    if status == 401:
        raise QuotaChannelsError(
            "cursor usage endpoint returned 401: re-run `agent login` to refresh Cursor CLI auth"
        )
    if status != 200:
        raise QuotaChannelsError(
            f"cursor usage endpoint returned {status}: {text[:200]}"
        )
    auto_remaining, api_remaining, reset_secs = parse_cursor_usage(
        text, now_fn=now_fn
    )
    return (
        format_cursor_name(auto_remaining, api_remaining, reset_secs),
        reset_secs,
        "Cursor",
    )


def run_grok_provider(
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> Tuple[str, float, str]:
    store = load_store()
    toks = store.get("providers", {}).get("xai-oauth", {}).get("tokens", {})
    access = toks.get("access_token")
    if not access:
        raise QuotaChannelsError("no xai-oauth access token in hermes auth store")
    status, body = fetch_grok_usage(access, http_fn=http_fn)
    if status == 401:
        access = refresh_xai_tokens(store, http_fn=http_fn)
        status, body = fetch_grok_usage(access, http_fn=http_fn)
    if status != 200:
        raise QuotaChannelsError(
            f"grok billing endpoint returned {status}: {body[:200]!r}"
        )
    remaining, reset_secs = parse_grok_usage(body, now_fn=now_fn)
    return format_grok_name(remaining, reset_secs), reset_secs, "Grok"


PROVIDER_RUNNERS = {
    "codex": run_codex_provider,
    "kimi": run_kimi_provider,
    "zai": run_zai_provider,
    "cursor": run_cursor_provider,
    "grok": run_grok_provider,
}


def plan_position_moves(
    entries: Sequence[Tuple[str, str, int]],
    guild_channels: Sequence[Mapping[str, Any]],
) -> List[dict]:
    ordered = sorted(entries, key=lambda item: item[2])
    channel_ids = {cid for _, cid, _ in entries}
    positions: Dict[str, Any] = {}
    for channel in guild_channels:
        cid = channel.get("id")
        if cid in channel_ids:
            positions[cid] = channel.get("position")
    if len(positions) != len(entries):
        raise QuotaChannelsError(
            f"expected {len(entries)} quota voice channels in guild, "
            f"found {len(positions)}"
        )
    slots = sorted(positions.values())
    moves: List[dict] = []
    for (_, cid, _), slot in zip(ordered, slots):
        if positions[cid] != slot:
            moves.append({"id": cid, "position": slot})
    return moves


def apply_position_moves(
    guild_id: str,
    moves: Sequence[dict],
    headers: dict,
    http_fn: HttpFn = default_http,
) -> bool:
    if not moves:
        return False
    req = urllib.request.Request(
        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
        data=json.dumps(list(moves)).encode(),
        headers=headers,
        method="PATCH",
    )
    status, text = http_text(req, http_fn=http_fn)
    if status == 429:
        raise QuotaChannelsError(
            f"discord channel-position PATCH rate-limited (429): {text[:200]}"
        )
    if status not in (200, 204):
        raise QuotaChannelsError(
            f"discord channel-position PATCH returned {status}: {text[:200]}"
        )
    return True


def fetch_guild_channels(
    guild_id: str,
    headers: dict,
    http_fn: HttpFn = default_http,
) -> List[Mapping[str, Any]]:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/guilds/{guild_id}/channels", headers=headers
    )
    status, text = http_text(req, http_fn=http_fn)
    if status != 200:
        raise QuotaChannelsError(
            f"discord guild channels fetch returned {status}: {text[:200]}"
        )
    try:
        guild_channels = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError("discord: invalid guild channels response JSON") from exc
    if not isinstance(guild_channels, list):
        raise QuotaChannelsError("discord: invalid guild channels response JSON")
    return guild_channels


def sort_voice_channels(
    config: dict,
    entries: Sequence[Tuple[str, str, int]],
    headers: dict,
    http_fn: HttpFn = default_http,
) -> bool:
    guild_channels = fetch_guild_channels(
        config["guild_id"], headers, http_fn=http_fn
    )
    moves = plan_position_moves(entries, guild_channels)
    return apply_position_moves(
        config["guild_id"], moves, headers, http_fn=http_fn
    )


def stored_ts_channel_id(
    state: Mapping[str, Any],
    category_id: str,
) -> Optional[str]:
    channels = state.get(TS_CHANNELS_KEY)
    if not isinstance(channels, Mapping):
        return None
    stored = channels.get(str(category_id))
    return None if stored is None else str(stored)


def ts_channel_matches(
    channel: Optional[Mapping[str, Any]],
    category_id: str,
) -> bool:
    """The managed channel is identified by ID only, never by name."""
    if not isinstance(channel, Mapping):
        return False
    if channel.get("type") != 2:
        return False
    return str(channel.get("parent_id") or "") == str(category_id)


def resolve_ts_channel(
    state: Mapping[str, Any],
    category_id: str,
    headers: dict,
    http_fn: HttpFn = default_http,
) -> Optional[str]:
    """Stored channel ID, but only while it is still a type-2 child of the category."""
    channel_id = stored_ts_channel_id(state, category_id)
    if channel_id is None:
        return None
    channel = fetch_channel_json(channel_id, headers, http_fn=http_fn)
    if not ts_channel_matches(channel, category_id):
        return None
    return channel_id


def create_ts_channel(
    guild_id: str,
    name: str,
    category_id: str,
    headers: dict,
    http_fn: HttpFn = default_http,
) -> str:
    # voice-channel creates carry exactly name/type/parent_id: Discord rejects
    # a topic on a voice channel with 400 code 50035 CHANNEL_TOPIC_INVALID
    req = urllib.request.Request(
        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
        data=json.dumps(
            {"name": name, "type": 2, "parent_id": str(category_id)}
        ).encode(),
        headers=headers,
        method="POST",
    )
    status, text = http_text(req, http_fn=http_fn)
    if status not in (200, 201):
        raise QuotaChannelsError(
            f"discord channel create returned {status}: {text[:200]}"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaChannelsError(
            "discord: invalid channel create response JSON"
        ) from exc
    if not isinstance(data, dict) or not data.get("id"):
        raise QuotaChannelsError("discord channel create response missing id")
    return str(data["id"])


def persist_ts_channel_id(category_id: str, channel_id: str) -> None:
    state = load_state()
    if not isinstance(state, dict):
        state = {}
    channels = state.get(TS_CHANNELS_KEY)
    if not isinstance(channels, dict):
        channels = {}
    channels[str(category_id)] = str(channel_id)
    state[TS_CHANNELS_KEY] = channels
    _write_state(state)


def plan_ts_bottom_move(
    channel_id: str,
    guild_channels: Sequence[Mapping[str, Any]],
    category_id: str,
) -> Optional[dict]:
    """Move for putting the managed channel last, or None if already bottom."""
    ours = str(channel_id)
    ours_position: Optional[int] = None
    others: List[int] = []
    for channel in guild_channels:
        if not isinstance(channel, Mapping):
            continue
        if str(channel.get("parent_id") or "") != str(category_id):
            continue
        position = channel.get("position") or 0
        if str(channel.get("id") or "") == ours:
            ours_position = position
        else:
            others.append(position)
    if not others:
        return None  # alone under the category: nothing to move below
    bottom = max(others)
    if ours_position is not None and ours_position > bottom:
        return None  # strictly below every sibling
    return {"id": ours, "position": bottom + 1}


def maintain_timestamp_channel(
    config: dict,
    headers: dict,
    *,
    http_fn: HttpFn = default_http,
) -> dict:
    """Idempotently keep one timestamp voice channel at the category bottom.

    The name always derives from the persisted last_quota_success, never from
    tick time; with no successful run yet the channel is named "pending".
    """
    category_id = str(config["category_id"])
    state = load_state()
    if not isinstance(state, dict):
        state = {}
    name = timestamp_channel_name(state.get("last_quota_success"))

    channel_id = resolve_ts_channel(state, category_id, headers, http_fn=http_fn)
    created = False
    if channel_id is None:
        channel_id = create_ts_channel(
            config["guild_id"], name, category_id, headers, http_fn=http_fn
        )
        persist_ts_channel_id(category_id, channel_id)
        created = True

    rename = (
        "unchanged"
        if created
        else rename_channel(channel_id, name, headers, http_fn=http_fn)
    )

    guild_channels = fetch_guild_channels(
        config["guild_id"], headers, http_fn=http_fn
    )
    move = plan_ts_bottom_move(channel_id, guild_channels, category_id)
    moved = False
    if move is not None:
        moved = apply_position_moves(
            config["guild_id"], [move], headers, http_fn=http_fn
        )

    return {
        "channel_id": channel_id,
        "name": name,
        "rename": rename,
        "created": created,
        "moved": moved,
    }


def quota_due(
    state: Mapping[str, Any],
    interval: int,
    force: bool,
    now_fn: NowFn = time.time,
) -> bool:
    if force:
        return True
    try:
        last = float(state.get("last_quota_success") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last <= 0:
        return True
    return now_fn() - last >= interval


def run_provider_quota(
    key: str,
    channel_id: str,
    headers: dict,
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> Tuple[str, float, str, str]:
    runner = PROVIDER_RUNNERS[key]
    name, reset_secs, label = runner(http_fn=http_fn, now_fn=now_fn)
    rename = rename_channel(channel_id, name, headers, http_fn=http_fn)
    return label, reset_secs, name, rename


def update_category(
    category_id: str,
    last_success: float,
    interval: int,
    headers: dict,
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> str:
    name = category_name(last_success, interval, now_fn=now_fn)
    return rename_channel(
        category_id,
        name,
        headers,
        skip_on_429=True,
        http_fn=http_fn,
    )


def run_tick(
    config: dict,
    *,
    force: bool = False,
    sleep_fn: SleepFn = time.sleep,  # kept for API compatibility; no longer called
    now_fn: NowFn = time.time,
    http_fn: HttpFn = default_http,
) -> dict:
    state = load_state()
    interval = config["quota_interval_seconds"]
    did_quota = quota_due(state, interval, force, now_fn=now_fn)

    provider_results: Dict[str, Any] = {}
    sorted_channels = False

    try:
        last = float(state.get("last_quota_success") or 0)
    except (TypeError, ValueError):
        last = 0.0

    headers = discord_headers()

    if did_quota:
        entries: List[Tuple[str, str, float]] = []
        for key, label, channel_id in config["providers"]:
            try:
                prov_label, reset_secs, channel_name, rename = run_provider_quota(
                    key, channel_id, headers, http_fn=http_fn, now_fn=now_fn
                )
            except Exception as exc:
                if isinstance(exc, QuotaChannelsError):
                    msg = str(exc)
                else:
                    msg = f"{type(exc).__name__}: {exc}"
                provider_results[label] = {"error": msg}
                continue
            provider_results[prov_label] = {
                "remaining": _remaining_from_name(channel_name, prov_label),
                "reset_seconds": reset_secs,
                "rename": rename,
            }
            entries.append((label, channel_id, reset_secs))
        if entries:
            sorted_channels = sort_voice_channels(
                config, entries, headers, http_fn=http_fn
            )
            last = save_state(now_fn=now_fn)

    category_status = update_category(
        config["category_id"],
        last,
        interval,
        headers,
        http_fn=http_fn,
        now_fn=now_fn,
    )

    timestamp_channel = maintain_timestamp_channel(
        config, headers, http_fn=http_fn
    )

    return {
        "success": True,
        "did_quota": did_quota,
        "providers": provider_results,
        "category": category_status,
        "sorted": sorted_channels,
        "timestamp_channel": timestamp_channel,
    }


def _remaining_from_name(channel_name: str, label: str) -> Any:
    if label == "Cursor":
        body = channel_name.split(":", 1)[1].strip()
        pct_part = body.split("\u2022", 1)[0].strip()
        auto, api = pct_part.split("/", 1)
        return {"auto": int(auto.rstrip("%")), "api": int(api.rstrip("%"))}
    body = channel_name.split(":", 1)[1].strip()
    pct = body.split("\u2022", 1)[0].strip().rstrip("%")
    return int(pct)
