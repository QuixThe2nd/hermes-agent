"""delegate-claude-agent — standalone Hermes plugin for Claude Code (GLM) delegation."""

from __future__ import annotations

if __package__:
    from .delegate_claude_agent.tool import (
        DELEGATE_CLAUDE_AGENT_SCHEMA,
        _handle_delegate_claude_agent,
        check_claude_agent_requirements,
    )
else:
    from delegate_claude_agent.tool import (
        DELEGATE_CLAUDE_AGENT_SCHEMA,
        _handle_delegate_claude_agent,
        check_claude_agent_requirements,
    )


def register(ctx) -> None:
    ctx.register_tool(
        name="delegate_claude_agent",
        toolset="delegation",
        schema=DELEGATE_CLAUDE_AGENT_SCHEMA,
        handler=_handle_delegate_claude_agent,
        check_fn=check_claude_agent_requirements,
        emoji="🤖",
    )
