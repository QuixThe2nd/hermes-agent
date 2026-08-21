# quota_channels

Discord voice-channel quota display for **Codex**, **Kimi**, **z.ai**, **Cursor**, and **Grok**. Renames configured voice channels with remaining quota percentages and a granular time-until-reset countdown (days at 2+ days out, then hours, then minutes), sorts channels by time until reset (ascending), and keeps a category channel label fresh between cron ticks.

## What it does

Each tick (typically every minute via cron):

1. **Quota gate** — provider API fetches run at most every `quota_interval_seconds` (default 30 minutes) unless forced. State lives in `HERMES_HOME/quota_channels_state.json`.
2. **On a quota run** — fetch all enabled providers, rename voice channels, sort them by time until reset, save state, sleep `post_quota_delay_seconds` (default 31s), then refresh the category label again.
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
  # Optional — disabled by default; omit entirely for pre-1.1 behavior
  token_usage:
    enabled: false
    channel_ids:
      codex: "VOICE_CHANNEL_ID"
      zai: "VOICE_CHANNEL_ID"
      cursor: "VOICE_CHANNEL_ID"
      # kimi/grok: no account-wide token API — static "no token API" label only
```

`enabled_providers` may also be a list, e.g. `["codex", "kimi"]`.

**Upgrade note:** No config changes are required when updating from a quota-only install. Existing quota channels keep updating automatically; token usage stays off unless you opt in with `token_usage.enabled: true`.

## Token usage channels (optional)

When `token_usage.enabled: true`, additional voice channels in the **same category** as the quota channels show **rolling 7-day consumed tokens** (`<Provider>: 226.6M tok/7d`). Token channels are ordered **after** the quota block in fixed canonical provider order (Codex, Kimi, z.ai, Cursor, Grok), including only providers with a mapped `token_usage.channel_ids` entry. The single Quotas category freshness label (`Quotas • Updated: …`) is unchanged. Token provider fetches are **quota-gated** — they run only on ticks where the quota gate is open (`did_quota`), at most every `quota_interval_seconds`.

| Provider | Source | Notes |
|----------|--------|-------|
| Codex | `GET …/wham/profiles/me` → sum latest 7 calendar-day `stats.daily_usage_buckets` | Stats may lag ~1 day (`stats_as_of`) |
| z.ai | `GET …/model-usage` with UTC `startTime`/`endTime` as `yyyy-MM-dd HH:mm:ss` | HTTP 200 with empty body is an error, not zero |
| Cursor | `POST …/GetAggregatedUsageEvents` (epoch-ms strings, now−7d..now) | Total = input + output only; cache tokens excluded |
| Kimi, Grok | — | Static `<Provider>: no token API`; **no** token-related provider HTTP call |

Per-provider isolation: fetch/parse/rename failures are independent (`updated` / `unchanged` / `unsupported` / `skipped` / `failed`). Transient fetch/auth/parse errors **preserve the prior channel name** (no rename to zero or placeholder). The token phase still runs when individual quota providers fail; a quota-phase exception is re-raised after tokens run (historical CLI behavior). Missing `token_usage.channel_ids` entries for an enabled provider → `skipped`, not an error.

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

When Grok's billing config omits the usage ratio (proto3 default 0) and carries a valid current usage-period marker (config field 8 type 1 or 2) with a valid reset timestamp, the Grok voice channel is renamed to `Grok: 100% • Nd left`. If the ratio is absent without that evidence, the provider fails honestly with an error instead of fabricating a percentage.

Per-provider failures are isolated: a failing provider appears as `{"error": "..."}` under `providers` in debug JSON and does not block other providers. If every provider fails on a quota run, state is not advanced and channel sorting is skipped.
