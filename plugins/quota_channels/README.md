# quota_channels

Discord voice-channel quota display for **Codex**, **Kimi**, **z.ai**, **Cursor**, and **Grok**. Renames configured voice channels with remaining quota percentages and days until reset, sorts channels by days remaining (ascending), and keeps a category channel label fresh between cron ticks.

## What it does

Each tick (typically every minute via cron):

1. **Quota gate** — provider API fetches run at most every `quota_interval_seconds` (default 30 minutes) unless forced. State lives in `HERMES_HOME/quota_channels_state.json`.
2. **On a quota run** — fetch all enabled providers, rename voice channels, sort them by days remaining, save state, sleep `post_quota_delay_seconds` (default 31s), then refresh the category label again.
3. **Every tick** — update the Quotas category name with a bucketed freshness label derived from seconds since the last successful full quota run.

Silent success for the headless CLI; failures print `quota-channels: <message>` and exit 1.

## Configuration

Add a `quota_channels:` section to `config.yaml`:

```yaml
quota_channels:
  guild_id: "YOUR_GUILD_ID"
  category_id: "YOUR_CATEGORY_CHANNEL_ID"
  quota_interval_seconds: 1800   # optional, default 1800 (30 min)
  post_quota_delay_seconds: 31   # optional, default 31
  channel_ids:
    codex: "VOICE_CHANNEL_ID"
    kimi: "VOICE_CHANNEL_ID"
    zai: "VOICE_CHANNEL_ID"
    cursor: "VOICE_CHANNEL_ID"
    grok: "VOICE_CHANNEL_ID"
  enabled_providers:             # optional; default all five enabled
    codex: true
    kimi: true
    zai: true
    cursor: true
    grok: true
```

`enabled_providers` may also be a list, e.g. `["codex", "kimi"]`.

Enable the toolset for sessions that should call the tool:

```bash
hermes tools   # enable "Quota Channels" / quota_channels
```

## Credentials (never commit real values)

| Provider | Location | Notes |
|----------|----------|-------|
| Discord bot | `HERMES_HOME/secrets/discord.env` | `DISCORD_BOT_TOKEN=` |
| Kimi | `HERMES_HOME/.env` | `KIMI_API_KEY=` |
| z.ai | `HERMES_HOME/secrets/zai.env` | `ZAI_API_KEY=` (raw Authorization header) |
| Codex | `HERMES_HOME/auth.json` | `providers.openai-codex.tokens` (OAuth refresh on 401) |
| Grok | `HERMES_HOME/auth.json` | `providers.xai-oauth.tokens` (OAuth refresh once on 401) |
| Cursor | `~/.config/cursor/auth.json` | `accessToken` JWT (re-run `agent login` on 401) |

## Cron setup

Run every minute without invoking the agent. Use an absolute path to `run.py` so the script bootstraps repo imports from any working directory; `python3 -m plugins.quota_channels.run` only works from the repo root (or with `PYTHONPATH` set).

```bash
hermes cron add \
  --schedule "every 1m" \
  --script "python3 /path/to/hermes-agent/plugins/quota_channels/run.py" \
  --no-agent \
  --name "quota-channels"
```

Force a quota fetch on one run:

```bash
python3 /path/to/hermes-agent/plugins/quota_channels/run.py --force-quota
```

Debug JSON on success:

```bash
python3 /path/to/hermes-agent/plugins/quota_channels/run.py --debug
```

## Tool

When the `quota_channels` toolset is enabled, the model may call:

- **`quota_channels_tick`** — one tick; optional `force: true` bypasses the quota gate.

Returns compact JSON, e.g. `{"success":true,"did_quota":true,"providers":{...},"category":"renamed","sorted":false}`.
