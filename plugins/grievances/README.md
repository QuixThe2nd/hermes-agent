# Grievances plugin

The `grievance` tool lets the agent file numbered complaints — with suggested fixes — to a
dedicated Discord channel. Use it for things the user should know that the agent cannot or
should not fix itself (personal notes, technical gripes, process complaints). It is **not**
for auto-resolvable bugs or workflow friction; use the **papercuts** plugin for those.

## Setup

1. Enable the plugin (on by default in this fork).
2. Ensure `$HERMES_HOME/.env` contains a non-empty `DISCORD_BOT_TOKEN`.
3. Enable the toolset for Discord sessions:

```bash
hermes tools enable grievances --platform discord
```

The bot needs the **Manage Channels** permission for first-time channel provisioning.

## First filing

On the first `file` action, if no channel exists yet, the plugin auto-runs setup: it creates
`#ai-grievances` (or your chosen name), posts a pinned welcome embed, and saves state under
`$HERMES_HOME/grievances/state.json`.

You can also provision explicitly with `action='setup'`.

## Multi-guild bots

If the bot is in more than one Discord server, setup returns the guild list and asks you to
re-run with `guild_id`. Single-guild bots auto-select the only server.

## vs Papercuts

| | Grievances | Papercuts |
|---|---|---|
| Audience | User should know / act | Agent workflow friction |
| Examples | "Stop renaming threads mid-turn" | "Config lookup required 3 extra steps" |
| Destination | Discord channel | Local JSONL journal |
| Auto-fix | No | Optional daily autofix cron |
