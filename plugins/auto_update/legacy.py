"""Legacy ``hermes-auto-update.*`` unit handling."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from plugins.auto_update.platform import InstallScope, build_systemctl_cmd

logger = logging.getLogger(__name__)

LEGACY_SERVICE = "hermes-auto-update.service"
LEGACY_TIMER = "hermes-auto-update.timer"

# Positive fingerprint for the only legacy unit bodies this plugin will disable.
# Unknown administrator-owned content is never deleted — only warned/refused.
LEGACY_KNOWN_SERVICE_BODY = """[Unit]
Description=Hermes automatic update (legacy bundled scheduler)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/false
X-Hermes-Legacy-Auto-Update=1

[Install]
WantedBy=multi-user.target
"""

LEGACY_KNOWN_TIMER_BODY = """[Unit]
Description=Hermes automatic update timer (legacy bundled scheduler)

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""

LEGACY_KNOWN_SERVICE_SHA256 = hashlib.sha256(
    LEGACY_KNOWN_SERVICE_BODY.encode("utf-8")
).hexdigest()
LEGACY_KNOWN_TIMER_SHA256 = hashlib.sha256(
    LEGACY_KNOWN_TIMER_BODY.encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class LegacyMigrationResult:
    disabled_units: tuple[str, ...]
    warnings: tuple[str, ...]
    refused: tuple[str, ...]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_unit(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _disable_unit(
    scope: InstallScope,
    unit: str,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]],
) -> bool:
    for args in (("disable", "--now", unit), ("stop", unit)):
        code, _, _ = run_systemctl(build_systemctl_cmd(scope, *args))
        if code not in (0, 5):
            return False
    return True


def migrate_legacy_units(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]],
) -> LegacyMigrationResult:
    disabled: list[str] = []
    warnings: list[str] = []
    refused: list[str] = []

    for unit, known_body, known_hash in (
        (LEGACY_SERVICE, LEGACY_KNOWN_SERVICE_BODY, LEGACY_KNOWN_SERVICE_SHA256),
        (LEGACY_TIMER, LEGACY_KNOWN_TIMER_BODY, LEGACY_KNOWN_TIMER_SHA256),
    ):
        path = scope.unit_dir / unit
        if not path.exists():
            continue
        content = _read_unit(path)
        if content is None:
            warnings.append(f"could not read legacy unit {path}")
            refused.append(unit)
            continue
        if _sha256_text(content) != known_hash:
            warnings.append(
                f"legacy unit {unit} has unknown content; refusing to modify administrator-owned file"
            )
            refused.append(unit)
            continue
        if _disable_unit(scope, unit, run_systemctl=run_systemctl):
            disabled.append(unit)
        else:
            warnings.append(f"failed to disable legacy unit {unit}")

    return LegacyMigrationResult(
        disabled_units=tuple(disabled),
        warnings=tuple(warnings),
        refused=tuple(refused),
    )


def duplicate_scheduler_present(scope: InstallScope) -> bool:
    return (scope.unit_dir / LEGACY_TIMER).exists()
