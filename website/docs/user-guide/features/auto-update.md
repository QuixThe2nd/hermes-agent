---
sidebar_position: 6
sidebar_label: "Auto Update"
title: "Unattended Auto Update"
description: "Safe scheduled Hermes updates on Linux/systemd when the agent is idle"
---

# Unattended Auto Update

The bundled **`auto_update`** backend plugin installs an independent systemd timer that runs the **stock** Hermes updater when your install looks idle:

```bash
hermes update --check
hermes update --yes
```

The plugin is a thin scheduler and policy layer. It does **not** reimplement backups, git operations, dependency installs, config migration, gateway restarts, or rollback logic — those stay inside `hermes update`.

:::note Platform support
Auto-update installs only on **Linux hosts with a functioning systemd**. Other platforms import the plugin cleanly and install nothing.
:::

## Quick start

```bash
hermes auto_update status
hermes auto_update enable      # write units + enable timer (never runs update immediately)
hermes auto_update disable     # stop timer; explicit disable survives upgrades
hermes auto_update reconcile   # idempotent unit refresh
```

On supported hosts the plugin ships **`default_enabled: true`** and reconciles its timer when loaded, unless you explicitly disable it.

## How scheduling works

| Piece | Name | Notes |
|---|---|---|
| Timer | `hermes-auto-updater.timer` | Hourly chances between **04:00–08:00 local**, `RandomizedDelaySec`, `Persistent=true` |
| Service | `hermes-auto-updater.service` | `Type=oneshot`, **no** `PartOf=` / `BindsTo=` coupling to the gateway |

First setup enables the **timer only** — it never starts the oneshot service immediately.

Install scope (system vs user systemd) is derived from your real Hermes paths and existing gateway unit metadata — not hardcoded usernames or checkout paths.

## Idle gate

Before invoking the updater, the oneshot runner re-checks idleness against a **read-only** `state.db` adapter:

- active assistant streaming (incomplete final assistant row)
- unanswered user work (interrupted last turn)
- recent message activity inside the configured idle window
- active compression locks

Missing or unreadable `state.db` fails **closed** as a quiet deferral. Lock contention or an in-flight manual update (`read_live_update`) also defer quietly.

`gateway/scale_to_zero.is_idle` is **not** used — it requires live gateway process state unavailable to a standalone oneshot.

## Configuration (`config.yaml`)

```yaml
auto_update:
  enabled: true
  idle_minutes: 8
  schedule_start_hour: 4
  schedule_end_hour: 8
  randomized_delay_sec: 3600
  notify_on_success: ""
  notify_on_failure: ""
```

- **`enabled: false`** or listing `auto_update` under `plugins.disabled` always wins and survives reconcile/upgrade.
- Empty notification strings keep success/failure quiet except for systemd journal logs.
- Non-empty strings append one line to `$HERMES_HOME/auto-update/notifications.log` (notification failures are non-fatal).

## Legacy units

Older installs may have shipped `hermes-auto-update.service` / `hermes-auto-update.timer`. The plugin:

- never deletes administrator-owned unit files
- disables **only** positively identified legacy bodies (exact known hash)
- refuses to enable duplicate schedulers when an unknown legacy timer remains

New units use the distinct prefix **`hermes-auto-updater.*`**.

## Safety boundaries

- One stock updater invocation per timer firing (`--check`, then `--yes` only if an update is available and idle checks still pass).
- Profile-safe nonblocking flock lock under `$HERMES_HOME/auto-update/.run.lock`.
- Atomic unit writes (`*.tmp` + `os.replace`) and byte-identical idempotent reconcile.

Disable any time:

```bash
hermes auto_update disable
# or
hermes config set auto_update.enabled false
hermes plugins disable auto_update
```
