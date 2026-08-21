"""Bundled backend plugin: safe unattended Hermes updates on Linux/systemd."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from plugins.auto_update.cli import auto_update_command, register_cli
    from plugins.auto_update.config import load_auto_update_config, plugin_explicitly_disabled
    from plugins.auto_update.platform import platform_supported
    from plugins.auto_update.systemd import reconcile_units

    ctx.register_cli_command(
        name="auto_update",
        help="Unattended Hermes update scheduler (Linux/systemd)",
        setup_fn=register_cli,
        handler_fn=auto_update_command,
        description=(
            "Install and manage an independent systemd timer that runs the stock "
            "`hermes update --check` / `hermes update --yes` flow when Hermes is idle."
        ),
    )

    if not platform_supported():
        return
    if plugin_explicitly_disabled():
        return
    try:
        cfg = load_auto_update_config()
        reconcile_units(cfg, enabled=bool(cfg.get("enabled", True)))
    except Exception as exc:
        logger.debug("auto_update reconcile on load skipped: %s", exc)
