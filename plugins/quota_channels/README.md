# quota_channels

Discord voice-channel quota display for **Codex**, **Kimi**, **z.ai**, **Cursor**, and **Grok**. Renames configured voice channels with remaining quota percentages and a granular time-until-reset countdown (days at 2+ days out, then hours, then minutes), sorts channels by time until reset (ascending), and keeps a category channel label fresh between cron ticks.

## What it does

Each tick (typically every minute via cron):

1. **Quota gate** — provider API fetches run at most every `quota_interval_seconds` (default 30 minutes) unless forced. State lives in `HERMES_HOME/quota_channels_state.json`.
2. **On a quota run** — fetch all enabled providers, rename voice channels, sort them by time until reset, and save state.
3. **Every tick** — update the Quotas category name once with the absolute local timestamp of the last successful quota run and either the next scheduled run time or `Due` when the interval has elapsed. Format: `Quotas • <day/month hour:minam/pm> • Next: <hour:minam/pm|Due>`.
4. **Every tick** — maintain the timestamp voice channel (below) after the category label update.

Silent success for the headless CLI; failures print `quota-channels: <message>` and exit 1.

## Timestamp channel

Each tick keeps one managed Discord **voice channel** at the bottom of the quota category showing when the last full quota refresh succeeded, e.g. `14/8 6:12pm` (day/month with no leading zeros, 12-hour clock with no leading zero, lowercase am/pm, zero-padded minutes — local time).

- **Naming** derives only from the persisted `last_quota_success` in the state file, never from tick time. Before any successful run (missing or zero timestamp) the channel is named `pending`. Renames go through the same changed-only path as the other channels.
- **Identity** lives in the state file under `ts_channels`, a map keyed by the category ID: `{"last_quota_success": 1755..., "ts_channels": {"CATEGORY_ID": "CHANNEL_ID"}}`. Channels are never adopted by name — on every tick the stored ID is reused only if it still exists as a type-2 (voice) channel whose parent is the configured category. If the stored channel vanished (404) or drifted (moved out of the category, changed type), exactly one replacement is created and the new ID is merged into state atomically alongside `last_quota_success`.
- **Creation** POSTs a body of exactly `name`, `type: 2`, and `parent_id`. A `topic` must never be included: Discord rejects it on voice-channel creates with HTTP 400, code 50035 `CHANNEL_TOPIC_INVALID`.
- **Positioning**: every tick the channel is checked against its category siblings and moved to the bottom via the guild channel-position PATCH when it is not strictly last — if any sibling ties the managed channel at the maximum position, a move to `max_position + 1` is sent; when the channel is alone in the category or its position is strictly greater than every sibling, no PATCH is sent. A 429 on that PATCH is a hard failure, matching the sort behavior.

The tick result reports this under `timestamp_channel`, e.g. `{"channel_id":"...","name":"14/8 6:12pm","rename":"unchanged","created":false,"moved":false}`.

## Configuration

Add a `quota_channels:` section to `config.yaml`:

```yaml
quota_channels:
  guild_id: "YOUR_GUILD_ID"
  category_id: "YOUR_CATEGORY_CHANNEL_ID"
  quota_interval_seconds: 1800   # optional, default 1800 (30 min)
  post_quota_delay_seconds: 31   # deprecated; ignored (kept for backward compatibility)
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

Returns compact JSON, e.g. `{"success":true,"did_quota":true,"providers":{...},"category":"renamed","sorted":false,"timestamp_channel":{...}}`.

When Grok's billing config omits the usage ratio (proto3 default 0) and carries a valid current usage-period marker (config field 8 type 1 or 2) with a valid reset timestamp, the Grok voice channel is renamed to `Grok: 100% • Nd left`. If the ratio is absent without that evidence, the provider fails honestly with an error instead of fabricating a percentage.

Per-provider failures are isolated: a failing provider appears as `{"error": "..."}` under `providers` in debug JSON and does not block other providers. If every provider fails on a quota run, state is not advanced and channel sorting is skipped.
