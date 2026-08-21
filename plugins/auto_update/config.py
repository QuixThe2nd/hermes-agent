"""Load ``auto_update`` settings from config.yaml with safe defaults."""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_IDLE_MINUTES = 8
DEFAULT_SCHEDULE_START_HOUR = 4
DEFAULT_SCHEDULE_END_HOUR = 8
DEFAULT_RANDOMIZED_DELAY_SEC = 3600


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def load_auto_update_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized auto-update settings.

    Explicit ``auto_update.enabled: false`` or ``plugins.disabled`` containing
    ``auto_update`` always wins — callers must check those gates first.
    """
    if raw is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
        except Exception:
            cfg = {}
        raw = cfg.get("auto_update") or {}

    if not isinstance(raw, Mapping):
        raw = {}

    start_hour = _coerce_int(
        raw.get("schedule_start_hour"), DEFAULT_SCHEDULE_START_HOUR, minimum=0
    )
    end_hour = _coerce_int(
        raw.get("schedule_end_hour"), DEFAULT_SCHEDULE_END_HOUR, minimum=0
    )
    if end_hour <= start_hour:
        end_hour = DEFAULT_SCHEDULE_END_HOUR
        start_hour = DEFAULT_SCHEDULE_START_HOUR

    return {
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "idle_minutes": _coerce_int(
            raw.get("idle_minutes"), DEFAULT_IDLE_MINUTES, minimum=1
        ),
        "schedule_start_hour": start_hour,
        "schedule_end_hour": end_hour,
        "randomized_delay_sec": _coerce_int(
            raw.get("randomized_delay_sec"), DEFAULT_RANDOMIZED_DELAY_SEC, minimum=0
        ),
        "notify_on_success": str(raw.get("notify_on_success") or "").strip(),
        "notify_on_failure": str(raw.get("notify_on_failure") or "").strip(),
    }


def plugin_explicitly_disabled(cfg: Mapping[str, Any] | None = None) -> bool:
    """True when config or the plugins deny-list disables this plugin."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
        except Exception:
            cfg = {}

    plugins = cfg.get("plugins") or {}
    disabled = plugins.get("disabled") or []
    if isinstance(disabled, list) and "auto_update" in disabled:
        return True

    section = cfg.get("auto_update") or {}
    if isinstance(section, Mapping) and section.get("enabled") is False:
        return True
    return False
