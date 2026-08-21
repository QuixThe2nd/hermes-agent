"""CLI for ``hermes auto_update {status,enable,disable,reconcile,run}``."""

from __future__ import annotations

import argparse
import sys

from hermes_constants import display_hermes_home

from plugins.auto_update.config import load_auto_update_config, plugin_explicitly_disabled
from plugins.auto_update.platform import detect_install_scope, platform_supported
from plugins.auto_update.lifecycle import reconcile_scheduler_on_load
from plugins.auto_update.runner import run_scheduled_update
from plugins.auto_update.systemd import (
    ReconcileResult,
    format_status,
    linger_warning,
    reconcile_units,
    timer_is_active,
)


def _effective_enabled() -> bool:
    return not plugin_explicitly_disabled() and load_auto_update_config()["enabled"]


def _management_failed(result: ReconcileResult, *, want_enabled: bool) -> bool:
    """True when reconcile did not achieve the intended scheduler state."""
    if not result.supported:
        return True
    operational_warnings = [
        w
        for w in result.warnings
        if w.startswith("failed to")
        or w == "timer enabled but not active"
    ]
    if want_enabled:
        if not result.enabled:
            return True
        return bool(operational_warnings)
    if result.enabled or result.timer_active:
        return True
    return bool(operational_warnings)


def cmd_status() -> int:
    cfg = load_auto_update_config()
    scope = detect_install_scope()
    warnings: list[str] = []
    linger = linger_warning(scope)
    if linger:
        warnings.append(linger)
    result = ReconcileResult(
        supported=platform_supported() and scope is not None,
        scope=scope,
        changed=False,
        enabled=_effective_enabled(),
        timer_active=False,
        legacy=(),
        warnings=tuple(warnings),
    )
    if result.supported and scope is not None:
        result = ReconcileResult(
            supported=True,
            scope=scope,
            changed=False,
            enabled=_effective_enabled(),
            timer_active=timer_is_active(scope),
            legacy=(),
            warnings=tuple(warnings),
        )
    print(format_status(result))
    print(f"  Config enabled: {'yes' if cfg['enabled'] else 'no'}")
    print(f"  Idle minutes: {cfg['idle_minutes']}")
    print(f"  Schedule: {cfg['schedule']}")
    print(f"  Randomized delay: {cfg['randomized_delay_sec']}s")
    if plugin_explicitly_disabled():
        print("  Explicit disable: yes (config/plugins.disabled)")
    return 0


def _save_enabled_flag(enabled: bool) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get("auto_update") or {})
    section["enabled"] = enabled
    cfg["auto_update"] = section
    save_config(cfg)


def cmd_enable() -> int:
    if not platform_supported():
        print(
            "Hermes auto-update requires Linux with systemd; nothing was installed."
        )
        return 1
    _save_enabled_flag(True)
    result = reconcile_scheduler_on_load()
    if result is None:
        result = reconcile_units(load_auto_update_config(), enabled=True)
    print(format_status(result))
    if _management_failed(result, want_enabled=True):
        return 1
    print(f"Scheduler installed under {display_hermes_home()}.")
    return 0


def cmd_disable() -> int:
    _save_enabled_flag(False)
    result = reconcile_scheduler_on_load()
    if result is None:
        result = reconcile_units(load_auto_update_config(), enabled=False)
    if result is not None and _management_failed(result, want_enabled=False):
        print(format_status(result))
        return 1
    print("Hermes auto-update disabled; timer stopped.")
    return 0


def cmd_reconcile() -> int:
    result = reconcile_scheduler_on_load()
    if result is None:
        enabled = _effective_enabled()
        result = reconcile_units(load_auto_update_config(), enabled=enabled)
    print(format_status(result))
    if not result.supported and platform_supported():
        return 1
    if _management_failed(result, want_enabled=_effective_enabled()):
        return 1
    return 0


def cmd_run() -> int:
    outcome = run_scheduled_update()
    if outcome.reason != "disabled":
        print(outcome.reason)
    return 0 if outcome.code == 0 else outcome.code


def register_management_cli(subparser: argparse.ArgumentParser):
    """Register management subcommands only (no ``run`` oneshot entrypoint)."""
    subs = subparser.add_subparsers(dest="auto_update_command")
    subs.add_parser("status", help="Show scheduler and config status")
    subs.add_parser("enable", help="Enable unattended updates and install the timer")
    subs.add_parser("disable", help="Disable unattended updates and stop the timer")
    subs.add_parser(
        "reconcile",
        help="Rewrite systemd units idempotently (respects explicit disable)",
    )
    return subs


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = register_management_cli(subparser)
    subs.add_parser(
        "run",
        help="Run one scheduled update attempt (systemd oneshot entrypoint)",
    )


def auto_update_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "auto_update_command", None)
    if sub == "status":
        return cmd_status()
    if sub == "enable":
        return cmd_enable()
    if sub == "disable":
        return cmd_disable()
    if sub == "reconcile":
        return cmd_reconcile()
    if sub == "run":
        return cmd_run()
    print("usage: hermes auto_update {status,enable,disable,reconcile,run}")
    return 2


def management_auto_update_command(args: argparse.Namespace) -> int:
    """Handler for disabled-management CLI — no ``run`` oneshot entrypoint."""
    sub = getattr(args, "auto_update_command", None)
    if sub == "run":
        print("usage: hermes auto_update {status,enable,disable,reconcile}")
        return 2
    if sub == "status":
        return cmd_status()
    if sub == "enable":
        return cmd_enable()
    if sub == "disable":
        return cmd_disable()
    if sub == "reconcile":
        return cmd_reconcile()
    print("usage: hermes auto_update {status,enable,disable,reconcile}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cmd_run())
