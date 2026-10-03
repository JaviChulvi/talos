# Operations and troubleshooting

[← Talos](../README.md) · [Documentation index](../README.md#documentation)

Inspect asynchronous operations, diagnose runtime failures, and maintain a source checkout. For release-bundle status, encrypted backups, restores, and updates, follow [Installation and recovery](installation.md#status-and-manual-maintenance). Run Compose commands from the repository root.

## Operations and diagnostics

`GET /api/v1/agents/{id}/availability` reads durable runtime/model/setup/connection
evidence, including when it was checked and expires. It never probes a service or
makes a model request. A ready container alone is not a verified available agent.
Configuration changes and new incarnations invalidate previous evidence; pending
role edits are shown separately from the currently applied configuration.
`GET /api/v1/status` reports recent worker/gateway heartbeats rather than assuming
configured services are alive. Unknown or expired evidence is explicit.

Create/start/stop/delete/dashboard return HTTP 202 and an operation ID. **Queued means accepted**, not completed. They require `Idempotency-Key`; replaying a key returns its existing operation, while conflicting work or a changed request returns HTTP 409. Inspect `GET /api/v1/operations/{id}` for the result.

| Request | Purpose |
| --- | --- |
| `POST /api/v1/agents` | Create with `display_name`, `employee_label`, and optional `runtime_mode` (`native`, the default, or `managed`) |
| `POST /api/v1/agents/{id}/dashboard` | Request a native UI handoff; poll its operation for `dashboard_url` |
| `GET /api/v1/agents` | List agents |
| `POST /api/v1/agents/{id}/start` or `/stop` | Change desired runtime state |
| `DELETE /api/v1/agents/{id}` | Delete the agent's owned runtime resources |
| `POST /api/v1/agents/{id}/diagnostic-runs` | Submit `{ "message": "hello" }` with `Idempotency-Key` |
| `GET /api/v1/agents/{id}/runs` | Read the latest 20 Talos conversation turns |
| `GET /api/v1/runs/{id}` | Inspect persisted diagnostic status and output |
| `GET /api/v1/runs/{id}/events?after=0` | Read ordered events; advance the cursor to the last sequence returned |
| `POST /api/v1/runs/{id}/cancel` | Request cancellation |

Every agent has a Chat tab in Talos. Native chat uses the runtime’s own model, tools, and persistent Talos conversation; configure providers in the native workspace under Settings. OpenClaw uses its gateway protocol; Hermes uses its structured native CLI inside the owned container. The latest 20 Talos messages are loaded from the server on reload. Conversations started separately in the native dashboard or channels remain separate. Interactive native approvals and setup are available in the full runtime interface. Only one unresolved diagnostic is admitted per managed agent. Messages are limited to 4,000 characters; event pages contain up to 100 records. Cancellation remains a request until confirmed by the runtime. **Unknown** means delivery or completion could not be confirmed; stop the agent, wait for the stop operation, then start it before sending another message. Restarting a worker never retries an uncertain message.

Stop closes gateway admission and preserves private agent state. A subsequent start creates a fresh container and credentials over the existing state volume. Gateway credentials expire after 30 days; stop/start renews them. Delete removes that agent's labeled resources and private credentials while retaining database history. Diagnostic input and output are stored in PostgreSQL; use synthetic data here. The dashboard remembers recent operation IDs in browser storage and clears them on logout; full history browsing is deferred.

## Source-checkout maintenance

**Stop or delete agents through Talos first and wait for their operations.** Then use `docker compose down` to stop the platform. Dynamically created agent containers are not Compose services: `down` alone does not stop them. Normal `down` preserves named volumes.

`docker compose down --volumes` destroys the platform database and worker credentials, but does not clean up dynamically created agent volumes. Do not use it to uninstall an installation with agents still present; delete agents through Talos while the database and worker are available. Avoid broad Docker prune commands.

For a source checkout, backups are manual. After stopping agents, stop API/worker/gateway/connector writes, then take a consistent PostgreSQL backup and volume snapshots, including `provider-secrets`, `setup-artifacts`, and `connection-secrets`. Preserve the `worker-state` volume, each agent's state/config volumes, `.env`, the checked-out commit, and runtime digest alongside the database. Default platform volume names are `talos_postgres-data` and `talos_worker-state`; dynamic agent resources carry `io.talos.project`, `io.talos.installation`, and `io.talos.agent` labels. Protect these backups: worker-state and configuration volumes contain workload credentials. A database dump alone cannot restore the installation.

For updates, retain those backups, keep the Compose project and installation identifiers unchanged, check out a reviewed commit, pull its approved runtime digest, and rerun `docker compose up --build -d`. Migrations run before services start. When upgrading an existing local HTTP installation,
add `TALOS_ADMIN_COOKIE_SECURE=false` to its `.env` explicitly, bootstrap the administrator
after migration, hard-refresh the dashboard to load the login UI, and sign in.
Existing agent/employee data is retained; the first
administrator bootstrap does not alter it. Reset requires host access and never deletes
agents, credentials, conversations, or usage.

Do not edit runtime pins without validating the corresponding lifecycle, authentication, and driver contracts. Source-checkout Compose deployments do not provide orchestrated backup/restore or platform rollback; restore the matching database, volumes, and code together. Release-bundle installations have explicit encrypted backup, restore, and pre-traffic update rollback commands in [Installation and recovery](installation.md#status-and-manual-maintenance). Neither path provides automatic native runtime upgrades or arbitrary native memory migrations.

## Troubleshooting

```sh
docker compose ps -a
docker compose logs migrate api worker gateway connector
```

- **Readiness fails:** inspect `migrate` and database health first. `/health/ready` requires the database schema to match the application migration head.
- **Start fails:** confirm the exact runtime image was pulled, Docker has free disk/memory, and the worker can reach its socket. Transient Docker/runtime operation failures retry up to five times. After correcting a terminal failure, start the agent again.
- **Docker storage is full:** if OpenClaw exits with `ENOSPC` during startup, Talos stops retrying and reports the storage error. Inspect Docker usage with `docker system df`, free space in Docker's own disk, then start the agent again. On Docker Desktop, free host disk space does not necessarily mean its VM has free space. Preserve agent and database volumes when cleaning up.
- **Database temporarily unavailable:** the worker waits and retries the same durable operation. Diagnostic delivery that cannot be confirmed becomes Unknown when the database returns; it is never automatically resent.
- **Unknown diagnostic:** stop the agent and wait for confirmation before starting it again. Repeated message submission cannot resolve uncertain upstream work.
- **Gateway access expires:** stop/start renews the 30-day incarnation identity. Never copy agent tokens into browser requests.
- **Missing worker credentials or ownership mismatch:** inspect the error and restore matching installation state. Talos refuses to adopt resources that do not match its recorded identity and labels.
- **Platform shutdown leaves agent containers running:** stop them through the dashboard before bringing the platform down, as described above.

While idle, the worker checks running agents and reconnects replaced platform containers. Failed probes mark an agent Degraded; a successful later probe restores Ready. These checks yield to queued lifecycle work.

The worker restarts automatically after a process failure; an explicit Compose stop keeps it stopped. Run only one worker per installation and preserve its private state. A lock prevents workers sharing that state directory from running concurrently; it does not coordinate separate worker volumes. `GET /api/v1/status` reports recent worker/gateway heartbeats; use operation results and service logs to investigate failures. A recent heartbeat alone does not verify employee delivery.
