# Restart-safe Kanban operations

Run the Kanban dispatcher independently from a profile gateway on hosts where gateway restarts must not interrupt work.

## Ownership model

- Set `kanban.dispatch_in_gateway: false` in the profile's `config.yaml`.
- Keep `kanban.notifier_in_gateway: true` (the default) so the gateway still delivers task results to connected chats.
- Install and enable `plugins/kanban/systemd/hermes-kanban-dispatcher.service` as a user unit.
- The unit uses `KillMode=process`: restarting the dispatcher stops only its main process. In-flight workers keep running and retain their persisted `task_runs` row, worker PID, heartbeat, and workspace.
- The dispatcher lock prevents two dispatchers from owning one board. The `ready -> running` transaction additionally enforces resource ownership.

## Filing tasks

Use `hermes-kanban-file-task` for automation. It requires an idempotency key and records the resource identity used by the dispatcher:

- `--project <slug>`: implementation work. The existing project integration creates an isolated deterministic worktree, so independent branches may run concurrently.
- `--repo /absolute/path`: shared-checkout work. Tasks for the same resolved `dir` path serialize.
- `--deployment-target host:service`: deploy/restart work. Tasks with the same non-empty target serialize even if their source worktrees differ.

Example:

    hermes-kanban-file-task \
      --title "Deploy Penny gateway" \
      --assignee penny \
      --project hermes-agent \
      --deployment-target olympus:hermes-gateway-penny \
      --idempotency-key deploy-penny-2026-07-16

A repeated call with the same idempotency key returns the original task rather than creating a duplicate.

## Restart and recovery semantics

1. Workers checkpoint progress with `kanban_heartbeat` and durable comments/artifacts. Heartbeats update both the task and current run.
2. A gateway restart does not own or kill workers when dispatch runs in the independent unit.
3. A dispatcher restart leaves workers alive because of `KillMode=process`. On startup the daemon reads persisted running tasks and PIDs; a live worker remains running and is not respawned.
4. If a worker actually disappears, crash detection reclaims the run and increments the task's consecutive-failure count. A service restart by itself does not consume retry budget.
5. Resource-lock deferrals leave tasks `ready`, add no failure count, and become eligible on a later tick after the holder completes or is reclaimed.

## Validation checklist

- `systemctl --user show hermes-kanban-dispatcher.service -p MainPID -p ActiveState -p SubState -p KillMode`
- `systemctl --user show hermes-gateway-<profile>.service -p MainPID -p ControlGroup`
- Confirm a worker PID survives `systemctl --user restart hermes-gateway-<profile>.service`.
- Confirm a running task keeps the same `current_run_id`, `worker_pid`, and failure count.
- Confirm a second task for the same deployment target stays `ready` until the first finishes.
