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

Plugin discovery and registration never write systemd units or run subprocesses.
On Linux/systemd hosts, gateway startup fires the generic `on_gateway_start`
lifecycle hook, which idempotently reconciles the timer (install/enable when
enabled; stop/disable when `auto_update.enabled: false`). The CLI verbs remain
the explicit management path for non-gateway installs and manual control.

## How scheduling works

| Piece | Name | Notes |
|---|---|---|
| Timer | `hermes-auto-updater.timer` | Off-hours local window **04:00–08:00** (`04,05,06,07:00:00`), `RandomizedDelaySec=1800`, `AccuracySec=1s`, `Persistent=true` |
| Service | `hermes-auto-updater.service` | `Type=oneshot`, **no** `PartOf=` / `BindsTo=` coupling to the gateway |

First setup enables the **timer only** — it never starts the oneshot service immediately. On first enable the timer stamp is pre-set so `Persistent=true` does not catch up missed slots from before install.

Install scope (system vs user systemd) is derived from your real Hermes paths and existing gateway unit metadata — not hardcoded usernames or checkout paths.

### User scope and linger

User-scoped timers stop when you log out unless **linger** is enabled for your account:

```bash
loginctl enable-linger "$USER"
```

`hermes auto_update status` and `enable` warn when user scope is selected but `/var/lib/systemd/linger/<user>` is absent (read-only check — no loginctl mutation).

## Idle gate

Before invoking the updater, the oneshot runner checks idleness against a **read-only** `state.db` adapter:

1. Once before `hermes update --check`
2. Again immediately before `hermes update --yes` (skipped if the second check fails)

Signals:

- active assistant streaming (incomplete final assistant row)
- unanswered user work (interrupted last turn)
- recent message activity inside the configured idle window
- active compression locks
- live session turn leases (`session_turn_leases.expires_at > now`)
- live delegated agents (`async_delegations.state IN ('running','finalizing')`)

Missing or unreadable `state.db` fails **closed** as a quiet deferral. Lock contention or an in-flight manual update (`read_live_update`) also defer quietly.

`gateway/scale_to_zero.is_idle` is **not** used — it requires live gateway process state unavailable to a standalone oneshot.

## Configuration (`config.yaml`)

```yaml
auto_update:
  enabled: true
  idle_minutes: 8
  schedule: "*-*-* 04,05,06,07:00:00"
  randomized_delay_sec: 1800
  accuracy_sec: "1s"
  notify_on_success: ""
  notify_on_failure: ""
```

- **`enabled: false`** stops and disables the timer on the next reconcile (gateway startup hook or `hermes auto_update reconcile` / `disable`) and survives upgrades. Prefer this when you need the CLI to stay available.
- Listing `auto_update` under **`plugins.disabled`** skips plugin registration entirely (CLI **and** the gateway-start hook), so it cannot stop an already-installed timer — run `hermes auto_update disable` or set `auto_update.enabled: false` **before** adding it to `plugins.disabled`.
- Optional `schedule_start_hour` / `schedule_end_hour` still override the default when set explicitly.
- Empty notification strings keep success/failure quiet except for systemd journal logs.
- Non-empty strings append one line to `$HERMES_HOME/auto-update/notifications.log` (notification failures are non-fatal).

## Legacy units

Older installs may have shipped `hermes-auto-update.service` / `hermes-auto-update.timer` plus a wrapper script. The plugin:

- never deletes administrator-owned unit files or wrapper scripts
- backs up positively identified legacy units under `$HERMES_HOME/auto-update/legacy-units/`
- disables **only** units matching the exact shipped legacy fingerprint (wrapper `ExecStart` line or full reference hash)
- refuses to enable duplicate schedulers when an unknown legacy timer remains enabled

New units use the distinct prefix **`hermes-auto-updater.*`**.

## Safety boundaries

- One stock updater invocation per timer firing (`--check`, then `--yes` only if an update is available and idle checks still pass).
- Profile-safe nonblocking flock lock under `$HERMES_HOME/auto-update/.run.lock` (lock file retained after release).
- Atomic unit writes (`*.tmp` + `os.replace`) and byte-identical idempotent reconcile.
- Disable/reconcile stop the **timer only** — never `systemctl stop hermes-auto-updater.service`.

Disable any time:

```bash
hermes auto_update disable
# or
hermes config set auto_update.enabled false
```

Logs: `journalctl --user -u hermes-auto-updater.service` (user scope) or `journalctl -u hermes-auto-updater.service` (system scope).
