# Talos

A self-hosted control plane for personal employee agents. The goal is to let companies manage agent integrations, permissions, credentials, spending, and offboarding.

**Current status: local Foundation prototype.** Talos creates and operates pinned OpenClaw containers, persists lifecycle operations and diagnostic results, and provides a small dashboard. Diagnostics use a deterministic fake model; no provider credentials or real integrations are needed. User authentication, employee access, business permissions, budgets, and Hermes support are future work.

## What works

- Create, inspect, start, stop, and delete an agent with a display name and employee label.
- One OpenClaw container, private internal network, and private state volume per agent.
- Durable PostgreSQL operations, idempotent requests, bounded retries, and adoption of owned Docker resources after a worker crash.
- Signed OpenClaw device pairing, diagnostic messages, ordered events, history support in the driver, and cancellation.
- Per-incarnation gateway credentials, checked against the current agent state and revocation/expiry on each fake-model request. Database failures deny access.
- Diagnostic delivery uncertainty is retained explicitly; messages are never automatically resent after a lost acknowledgment.

This prototype is for one organization with trusted host administrators. Keep the unauthenticated dashboard/API local. Agent containers have no published ports or Docker socket; only the trusted worker controls Docker. Containers share a host kernel, and Foundation does not establish the later enterprise permission or hostile-tenant isolation guarantees.

## Stack and layout

| Component | Implementation |
| --- | --- |
| Dashboard | React, TypeScript, Vite, shadcn/ui, Tailwind CSS; REST polling |
| API | Python 3.13, FastAPI, Pydantic; serves the frontend production build |
| Persistence | PostgreSQL 17, SQLAlchemy, Alembic |
| Worker | Python and Docker SDK; one worker per installation |
| Gateway | Separate FastAPI process; authenticated synthetic model fixture |
| Runtime | OpenClaw 2026.9.6, pinned by image digest; Gateway protocol v4 |
| Packaging | uv, pnpm, Docker Compose |

`frontend/` contains the dashboard; `backend/` owns the API and database models; `worker/` owns Docker and OpenClaw control; `gateway/` owns workload authentication and the fake model. `compose.yaml` starts the platform, `deploy/` contains its image build and runtime pin, and `tests/` contains focused backend and runtime checks. Node.js is used for frontend tooling; OpenClaw carries its own runtime dependencies.

## Run locally

Use Docker Engine with Compose on Linux, or Docker Desktop for development, plus Python 3 to read the runtime pin. Allow disk space for the pinned runtime image and persistent volumes; each agent has a 2 GiB memory limit. Run these commands from the repository root:

```sh
cp .env.example .env
docker pull "$(python3 -c 'import json; print(json.load(open("deploy/runtimes/openclaw.json"))["image"])')"
docker compose up --build -d
docker compose ps
```

Change the example database password in `.env` before retaining data. Open [http://127.0.0.1:8000](http://127.0.0.1:8000). PostgreSQL and the gateway are not published; the API binds to host loopback. The migration service must complete successfully before the application services start. The worker uses the already-pulled runtime digest; it does not pull arbitrary agent images.

For a remote Linux development host, keep that binding and use a tunnel:

```sh
ssh -N -L 8000:127.0.0.1:8000 user@your-host
```

In the dashboard, create an agent, wait for **Stopped**, start it, and wait for **Ready**. Send a short synthetic diagnostic message; include `[slow]` to exercise cancellation. A new start request on an already-ready agent is rejected: stop it first.

## Operations and diagnostics

Create/start/stop/delete return HTTP 202 and an operation ID. **Queued means accepted**, not completed. They require `Idempotency-Key`; replaying a key returns its existing operation, while conflicting work or a changed request returns HTTP 409. Inspect `GET /api/v1/operations/{id}` for the result.

| Request | Purpose |
| --- | --- |
| `POST /api/v1/agents` | Create with `display_name` and `employee_label` |
| `GET /api/v1/agents` | List agents |
| `POST /api/v1/agents/{id}/start` or `/stop` | Change desired runtime state |
| `DELETE /api/v1/agents/{id}` | Delete the agent's owned runtime resources |
| `POST /api/v1/agents/{id}/diagnostic-runs` | Submit `{ "message": "hello" }` with `Idempotency-Key` |
| `GET /api/v1/runs/{id}` | Inspect persisted diagnostic status and output |
| `GET /api/v1/runs/{id}/events?after=0` | Read ordered events; advance the cursor to the last sequence returned |
| `POST /api/v1/runs/{id}/cancel` | Request cancellation |

Only one unresolved diagnostic is admitted per agent. Messages are limited to 4,000 characters; event pages contain up to 100 records. Cancellation remains a request until confirmed by the runtime. **Unknown** means delivery or completion could not be confirmed; stop the agent, wait for the stop operation, then start it before sending another message. Restarting a worker never retries an uncertain message.

Stop closes gateway admission and preserves private agent state. A subsequent start creates a fresh container and credentials over the existing state volume. Gateway credentials expire after 30 days; stop/start renews them. Delete removes that agent's labeled resources and private credentials while retaining database history. Diagnostic input and output are stored in PostgreSQL; use synthetic data here. The dashboard remembers recent operation/run IDs in browser storage; full history browsing is deferred.

## Local checks

Host development uses Python 3.13, uv 0.11.21, Node 22.13 or later, and pnpm 11.19.0:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -m 'not integration'
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend lint
pnpm --dir frontend typecheck
pnpm --dir frontend build
```

Run `pnpm --dir frontend dev` for hot reload while Compose is running. Vite binds to loopback and proxies API requests to port 8000. UI verification is manual. There are no GitHub Actions workflows, browser-test dependencies, or end-to-end test suites.

The focused OpenClaw protocol check uses two real containers, synthetic model responses, and normal named volumes by default. Its trusted runner needs Docker access and removes its own runtime resources:

```sh
docker build --target verification -f deploy/Dockerfile -t talos-verification:local .
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock talos-verification:local
```

PostgreSQL tests create random schemas and drop only those schemas afterward. The following commands use a disposable database and run the API, recovery, diagnostic, and Docker-resource checks inside the Linux verification container. `--network host` here assumes native Linux:

```sh
docker run --detach --name talos-test-db \
  --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_PASSWORD=talos-test-password \
  postgres:17-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652
docker exec talos-test-db pg_isready -h 127.0.0.1 -U postgres
```

Wait until `pg_isready` reports accepting connections, then run:

```sh
docker run --rm --network host \
  --env TALOS_TEST_DATABASE_URL=postgresql+psycopg://postgres:talos-test-password@127.0.0.1:55432/postgres \
  --env TALOS_TEST_DOCKER=1 \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  talos-verification:local python -m pytest -q
docker rm --force --volumes talos-test-db
```

For an existing disposable PostgreSQL instance, set `TALOS_TEST_DATABASE_URL` and run `uv run pytest tests/integration`; add `TALOS_TEST_DOCKER=1` for actual Docker-resource checks. Default Compose does not expose its database to host tests.

RAM-backed test options (`TALOS_PROOF_RAM_VOLUMES=1` and `TALOS_TEST_TMPFS_VOLUMES=1`) exist for constrained development machines. Such runs verify process-level behavior, not physical disk or Docker-daemon restart persistence. Linux-container checks also do not establish a clean native-Linux installation or production readiness. A disk-backed backup/restore and daemon-restart acceptance run remains necessary before retaining important data.

## Stop, back up, and update

**Stop or delete agents through Talos first and wait for their operations.** Then use `docker compose down` to stop the platform. Dynamically created agent containers are not Compose services: `down` alone does not stop them. Normal `down` preserves named volumes.

`docker compose down --volumes` destroys the platform database and worker credentials, but does not clean up dynamically created agent volumes. Do not use it to uninstall an installation with agents still present; delete agents through Talos while the database and worker are available. Avoid broad Docker prune commands.

Backups are manual. After stopping agents, stop API/worker/gateway writes, then take a consistent PostgreSQL backup and volume snapshots. Preserve the `worker-state` volume, each agent's state/config volumes, `.env`, the checked-out commit, and runtime digest alongside the database. Default platform volume names are `talos_postgres-data` and `talos_worker-state`; dynamic agent resources carry `io.talos.project`, `io.talos.installation`, and `io.talos.agent` labels. Protect these backups: worker-state and configuration volumes contain workload credentials. A database dump alone cannot restore the installation.

For updates, retain those backups, keep the Compose project and installation identifiers unchanged, check out a reviewed commit, pull its approved runtime digest, and rerun `docker compose up --build -d`. Migrations run before services start. Do not edit the pin to a newer OpenClaw image without updating and validating its driver contract. Automated rollback, runtime upgrades, and backup/restore orchestration are not implemented; restore the matching database, volumes, and code together if rollback is needed.

## Troubleshooting

```sh
docker compose ps -a
docker compose logs migrate api worker gateway
```

- **Readiness fails:** inspect `migrate` and database health first. `/health/ready` requires the database schema to match the application migration head.
- **Start fails:** confirm the exact runtime image was pulled, Docker has free disk/memory, and the worker can reach its socket. Lifecycle operations retry at most five times. After correcting a terminal failure, stop/start explicitly.
- **Unknown diagnostic:** stop the agent and wait for confirmation before starting it again. Repeated message submission cannot resolve uncertain upstream work.
- **Gateway access expires:** stop/start renews the 30-day incarnation identity. Never copy agent tokens into browser requests.
- **Missing worker credentials or ownership mismatch:** inspect the error and restore matching installation state. Talos refuses to adopt resources that do not match its recorded identity and labels.
- **Platform shutdown leaves agent containers running:** stop them through the dashboard before bringing the platform down, as described above.

The worker restarts automatically after a process failure; an explicit Compose stop keeps it stopped. Run only one worker per installation and preserve its private state. A lock prevents workers sharing that state directory from running concurrently; it does not coordinate separate worker volumes. Dashboard worker/gateway capability labels are not live heartbeats—use operation results and service logs.
