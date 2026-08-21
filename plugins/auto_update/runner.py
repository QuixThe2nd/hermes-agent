"""Scheduled update runner — invokes stock ``hermes update`` only."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from hermes_cli.relaunch import resolve_hermes_bin
from hermes_cli.update_lock import read_live_update

from plugins.auto_update.config import load_auto_update_config, plugin_explicitly_disabled
from plugins.auto_update.idle import IdleSnapshot, evaluate_idle
from plugins.auto_update.lock import nonblocking_run_lock
from plugins.auto_update.notify import emit_notification

logger = logging.getLogger(__name__)

UPDATE_CHECK_ARGV = ("update", "--check")
UPDATE_APPLY_ARGV = ("update", "--yes")
UP_TO_DATE_MARKERS = ("already up to date", "✓ already up to date")


@dataclass(frozen=True)
class RunOutcome:
    code: int
    reason: str


def build_stock_updater_argv(mode: str) -> list[str]:
    """Return the exact public updater argv surface — no internal modules."""
    hermes_bin = resolve_hermes_bin()
    if hermes_bin:
        if mode == "check":
            return [hermes_bin, *UPDATE_CHECK_ARGV]
        return [hermes_bin, *UPDATE_APPLY_ARGV]
    import sys

    if mode == "check":
        return [sys.executable, "-m", "hermes_cli.main", *UPDATE_CHECK_ARGV]
    return [sys.executable, "-m", "hermes_cli.main", *UPDATE_APPLY_ARGV]


def _check_output_indicates_update_available(text: str) -> bool:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in UP_TO_DATE_MARKERS):
        return False
    return "update available" in lowered or "behind" in lowered


def run_subprocess(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )


def run_scheduled_update(
    *,
    cfg: dict | None = None,
    evaluate_idle_fn: Callable[..., IdleSnapshot] = evaluate_idle,
    run_cmd: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = run_subprocess,
    read_live_update_fn: Callable = read_live_update,
) -> RunOutcome:
    if plugin_explicitly_disabled():
        return RunOutcome(0, "disabled")

    settings = cfg or load_auto_update_config()
    if not settings.get("enabled", True):
        return RunOutcome(0, "disabled")

    if read_live_update_fn() is not None:
        return RunOutcome(0, "update_in_progress")

    with nonblocking_run_lock() as locked:
        if not locked:
            return RunOutcome(0, "lock_contention")

        idle = evaluate_idle_fn(idle_minutes=int(settings["idle_minutes"]))
        if not idle.idle:
            return RunOutcome(0, f"not_idle:{idle.blockers[0].code}")

        idle_recheck = evaluate_idle_fn(idle_minutes=int(settings["idle_minutes"]))
        if not idle_recheck.idle:
            return RunOutcome(0, f"not_idle_recheck:{idle_recheck.blockers[0].code}")

        check_argv = build_stock_updater_argv("check")
        check = run_cmd(check_argv)
        combined = "\n".join(filter(None, (check.stdout, check.stderr)))
        if check.returncode != 0 and not _check_output_indicates_update_available(combined):
            logger.info("auto-update check failed quietly: rc=%s", check.returncode)
            return RunOutcome(0, "check_failed")

        if not _check_output_indicates_update_available(combined):
            return RunOutcome(0, "no_update")

        apply_argv = build_stock_updater_argv("apply")
        apply = run_cmd(apply_argv)
        if apply.returncode == 0:
            emit_notification(settings.get("notify_on_success", ""))
            return RunOutcome(0, "updated")
        emit_notification(settings.get("notify_on_failure", ""))
        logger.warning(
            "auto-update apply failed: rc=%s stderr=%s",
            apply.returncode,
            (apply.stderr or "")[:500],
        )
        return RunOutcome(apply.returncode, "apply_failed")
