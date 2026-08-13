"""delegate-cursor-agent — standalone Hermes plugin for Cursor Agent CLI delegation."""

from __future__ import annotations

if __package__:
    from .delegate_cursor_agent.tool import (
        CURSOR_AGENT_SCHEMA,
        _handle_delegate_cursor_agent,
        check_cursor_agent_requirements,
    )
else:
    from delegate_cursor_agent.tool import (
        CURSOR_AGENT_SCHEMA,
        _handle_delegate_cursor_agent,
        check_cursor_agent_requirements,
    )


def register(ctx) -> None:
    ctx.register_tool(
        name="delegate_cursor_agent",
        toolset="delegation",
        schema=CURSOR_AGENT_SCHEMA,
        handler=_handle_delegate_cursor_agent,
        check_fn=check_cursor_agent_requirements,
        emoji="🖥️",
    )
