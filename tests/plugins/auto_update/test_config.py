"""Auto-update config defaults and schedule resolution."""

from __future__ import annotations

from plugins.auto_update.config import (
    DEFAULT_RANDOMIZED_DELAY_SEC,
    load_auto_update_config,
    plugin_explicitly_disabled,
    resolve_schedule,
)


def test_default_schedule_is_off_hours_with_randomized_delay():
    cfg = load_auto_update_config({})
    assert cfg["schedule"] == "*-*-* 04,05,06,07:00:00"
    assert cfg["randomized_delay_sec"] == DEFAULT_RANDOMIZED_DELAY_SEC
    assert cfg["randomized_delay_sec"] == 1800
    assert cfg["accuracy_sec"] == "1s"


def test_explicit_hour_window_overrides_default():
    schedule = resolve_schedule({"schedule_start_hour": 4, "schedule_end_hour": 8})
    assert schedule == "*-*-* 04,05,06,07:00:00"


def test_explicit_schedule_calendar_wins():
    custom = "*-*-* 02:15:00"
    schedule = resolve_schedule({"schedule": custom})
    assert schedule == custom


def test_enabled_string_false_counts_as_disabled():
    assert plugin_explicitly_disabled({"auto_update": {"enabled": "false"}}) is True


def test_enabled_string_true_is_not_explicit_disable():
    assert plugin_explicitly_disabled({"auto_update": {"enabled": "true"}}) is False
