"""Dev Pipeline plugin — durable automated development jobs.

Registers ``delegate_development`` (toolset ``dev-pipeline``): submit a repo +
task and Hermes plans via the MoA council, executes bounded work through the
Cursor CLI, verifies mechanically, reviews with Kimi K3 + Grok 4.5, and opens
a draft PR on pass. Jobs live in the Kanban DB and survive gateway/executor
restarts and host reboots.

The executor service is NOT installed by this plugin — see systemd/README.md.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Register the delegate_development tool. Called once by the plugin loader."""
    from plugins.dev_pipeline.tool import (
        DELEGATE_DEVELOPMENT_SCHEMA,
        _handle_delegate_development,
        check_dev_pipeline_requirements,
    )

    ctx.register_tool(
        name="delegate_development",
        toolset="dev-pipeline",
        schema=DELEGATE_DEVELOPMENT_SCHEMA,
        handler=_handle_delegate_development,
        check_fn=check_dev_pipeline_requirements,
        emoji="🏗️",
    )
