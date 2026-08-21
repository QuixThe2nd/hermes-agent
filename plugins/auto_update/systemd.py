"""Systemd unit rendering and idempotent reconciliation."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from hermes_constants import display_hermes_home, get_hermes_home

from plugins.auto_update.legacy import duplicate_scheduler_present, migrate_legacy_units
from plugins.auto_update.platform import (
    InstallScope,
    build_systemctl_cmd,
    detect_install_scope,
    platform_supported,
    profile_cli_args,
    unit_exec_start_argv,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "hermes-auto-updater.service"
TIMER_NAME = "hermes-auto-updater.timer"


@dataclass(frozen=True)
class ReconcileResult:
    supported: bool
    scope: InstallScope | None
    changed: bool
    enabled: bool
    timer_active: bool
    legacy: tuple[str, ...]
    warnings: tuple[str, ...]


def service_unit_path(scope: InstallScope) -> Path:
    return scope.unit_dir / SERVICE_NAME


def timer_unit_path(scope: InstallScope) -> Path:
    return scope.unit_dir / TIMER_NAME


def _calendar_hours(start_hour: int, end_hour: int) -> str:
    hours = list(range(start_hour, end_hour))
    if not hours:
        hours = [4, 5, 6, 7]
    return ",".join(f"{hour:02d}" for hour in hours)


def render_service_unit(
    *,
    hermes_home: str,
    exec_start: Sequence[str],
    scope: InstallScope | None = None,
) -> str:
    exec_line = " ".join(exec_start)
    profile = profile_cli_args()
    profile_env = ""
    if profile:
        profile_env = f'Environment="HERMES_PROFILE={profile[-1]}"\n'
    identity = ""
    wanted_by = "WantedBy=multi-user.target"
    if scope and not scope.system:
        wanted_by = "WantedBy=default.target"
    elif scope and scope.system:
        try:
            import pwd

            st = Path(hermes_home).stat()
            user = pwd.getpwuid(st.st_uid).pw_name
            group = pwd.getpwuid(st.st_uid).pw_name
            identity = f"User={user}\nGroup={group}\n"
        except (ImportError, KeyError, OSError):
            pass
    return f"""[Unit]
Description=Hermes unattended update (oneshot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{identity}ExecStart={exec_line}
WorkingDirectory={hermes_home}
Environment="HERMES_HOME={hermes_home}"
{profile_env}StandardOutput=journal
StandardError=journal

[Install]
{wanted_by}
"""


def render_timer_unit(
    *,
    start_hour: int,
    end_hour: int,
    randomized_delay_sec: int,
) -> str:
    hours = _calendar_hours(start_hour, end_hour)
    return f"""[Unit]
Description=Hermes unattended update schedule

[Timer]
OnCalendar=*-*-* {hours}:00:00
RandomizedDelaySec={randomized_delay_sec}
Persistent=true
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def atomic_write(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


def write_units_if_changed(
    scope: InstallScope,
    *,
    service_body: str,
    timer_body: str,
) -> bool:
    changed = False
    for path, body in (
        (service_unit_path(scope), service_body),
        (timer_unit_path(scope), timer_body),
    ):
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if existing == body:
            continue
        atomic_write(path, body)
        changed = True
    return changed


def default_systemctl_runner(args: Sequence[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def timer_is_active(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] = default_systemctl_runner,
) -> bool:
    code, out, _ = run_systemctl(
        build_systemctl_cmd(scope, "is-active", TIMER_NAME)
    )
    return code == 0 and out.strip() == "active"


def timer_is_enabled(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] = default_systemctl_runner,
) -> bool:
    code, out, _ = run_systemctl(
        build_systemctl_cmd(scope, "is-enabled", TIMER_NAME)
    )
    return code == 0 and out.strip() in {"enabled", "static"}


def disable_timer(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] = default_systemctl_runner,
) -> None:
    run_systemctl(build_systemctl_cmd(scope, "disable", "--now", TIMER_NAME))
    run_systemctl(build_systemctl_cmd(scope, "stop", SERVICE_NAME))


def reconcile_units(
    cfg: Mapping[str, object],
    *,
    enabled: bool,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] = default_systemctl_runner,
    scope: InstallScope | None = None,
) -> ReconcileResult:
    if not platform_supported():
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=(),
        )

    selected = scope or detect_install_scope()
    if selected is None:
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=("systemd user manager unavailable for this install",),
        )

    if not enabled:
        disable_timer(selected, run_systemctl=run_systemctl)
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=(),
        )

    hermes_home = str(get_hermes_home().resolve())
    service_body = render_service_unit(
        hermes_home=hermes_home,
        exec_start=unit_exec_start_argv(),
        scope=selected,
    )
    timer_body = render_timer_unit(
        start_hour=int(cfg.get("schedule_start_hour", 4)),
        end_hour=int(cfg.get("schedule_end_hour", 8)),
        randomized_delay_sec=int(cfg.get("randomized_delay_sec", 3600)),
    )

    changed = write_units_if_changed(
        selected,
        service_body=service_body,
        timer_body=timer_body,
    )
    if changed:
        run_systemctl(build_systemctl_cmd(selected, "daemon-reload"))

    legacy = migrate_legacy_units(selected, run_systemctl=run_systemctl)
    warnings = list(legacy.warnings)
    if duplicate_scheduler_present(selected):
        warnings.append(
            "legacy hermes-auto-update.timer still present; refusing duplicate schedulers"
        )
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=changed,
            enabled=False,
            timer_active=False,
            legacy=legacy.disabled_units,
            warnings=tuple(warnings),
        )

    # Enable/start TIMER ONLY — never start the oneshot service immediately.
    code, _, err = run_systemctl(
        build_systemctl_cmd(selected, "enable", "--now", TIMER_NAME)
    )
    if code != 0:
        warnings.append(f"failed to enable timer: {err.strip() or code}")

    return ReconcileResult(
        supported=True,
        scope=selected,
        changed=changed,
        enabled=True,
        timer_active=timer_is_active(selected, run_systemctl=run_systemctl),
        legacy=legacy.disabled_units,
        warnings=tuple(warnings),
    )


def format_status(result: ReconcileResult) -> str:
    home = display_hermes_home()
    if not result.supported:
        return (
            "Hermes auto-update scheduler is unavailable on this platform "
            "(requires Linux with a functioning systemd installation)."
        )
    lines = [
        f"Hermes auto-update ({home})",
        f"  Timer unit: {TIMER_NAME}",
        f"  Service unit: {SERVICE_NAME}",
        f"  Scope: {'system' if result.scope and result.scope.system else 'user'}",
        f"  Enabled: {'yes' if result.enabled else 'no'}",
        f"  Timer active: {'yes' if result.timer_active else 'no'}",
    ]
    if result.legacy:
        lines.append(f"  Legacy units disabled: {', '.join(result.legacy)}")
    for warning in result.warnings:
        lines.append(f"  Warning: {warning}")
    return "\n".join(lines)
