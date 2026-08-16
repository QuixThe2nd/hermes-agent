# Hermes dev-pipeline executor (systemd)

Shipped unit file for the durable dev-pipeline executor. **Not installed automatically** — copy and edit paths for your host.

## Prerequisites

- `dev_pipeline.enabled: true` in `~/.hermes/config.yaml` (default is **false**; the service refuses to claim work when disabled).
- Cursor Agent CLI (`agent`) on `PATH`.
- `gh` authenticated for draft PR creation (executor credentials only — attempts never see tokens).

## Install

```bash
sudo cp packaging/dev-executor/hermes-dev-executor.service /etc/systemd/system/
# Edit ExecStart to your venv python, e.g. /root/hermes-agent/.venv/bin/python
sudo systemctl daemon-reload
sudo systemctl enable hermes-dev-executor
sudo systemctl start hermes-dev-executor
```

## Verify

```bash
systemctl status hermes-dev-executor
journalctl -u hermes-dev-executor -f
```

Logs also land under the Kanban board logs root (`<hermes_home>/kanban/boards/dev/logs/<task_id>/`).

## Cancellation

Blocking a running task (`hermes kanban block <id>`) causes the executor to stop the active attempt unit, record `cancelled_by_user`, and leave the workspace intact for evidence.

## Attempt units

Each implementation attempt runs as a separate transient unit `hermes-dev-<task_id>-<run_id>`, spawned via `systemd-run` outside the executor cgroup. That is why `KillMode=mixed` on the executor service is acceptable.
