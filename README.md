# Talos

A self-hosted platform for managing personal employee agents while a company controls their integrations, permissions, credentials, spending, and lifecycle.

The project is planned as an open-source platform inspired by Coolify's approach to deploying and operating workloads. It focuses on giving each employee a useful personal agent with company-managed access to external services.

**Status:** Foundation development. This branch provides the local service stack and status screen. Agent lifecycle, permissions, and integrations are being added in dependent PRs; this is not ready for shared employee access.

## Project Overview

Administrators will be able to create agents, assign permission profiles, manage integrations, set spending limits, inspect activity, and revoke access. Employees will eventually interact with their own agents through a web chat interface and personalize approved preferences without changing company permissions.

The first version will run on a single customer-managed Linux machine. Each agent will use a pinned OpenClaw image, its own Docker container, and private persistent state. A small runtime adapter will keep OpenClaw-specific behavior separate from platform and container management.

The integration gateway is a central part of the design. It will keep provider credentials outside agent containers, authenticate each agent, and check its current permissions before performing an external operation. Agents will receive only scoped platform credentials and permitted results. The model gateway will similarly enforce approved models and spending limits.

## Initial Scope

- One organization on one Linux host.
- OpenClaw as the first supported agent runtime.
- Local Docker management implemented in Python.
- Agent creation, inspection, start, stop, deletion, and access revocation.
- Private persistent state for each agent.
- A React dashboard and employee chat interface.
- A separate gateway for integration credentials, action permissions, and model budgets.
- Durable background operations and activity records in PostgreSQL.
- A documented installation, backup, and pinned-update workflow.

User authentication is deferred for the local development prototype. Its dashboard and administrative API must remain local-only, with an SSH tunnel available for remote development. Employee access and administrator/employee role enforcement will require authentication before a shared rollout.

Per-agent gateway authentication remains part of the initial design. Agent containers must not be able to reach the unauthenticated administrative API, access the Docker socket, obtain provider credentials, or bypass gateway policy through unrestricted network access. Only the trusted Docker worker will control the local Docker daemon.

Containers on one host share a kernel. This initial deployment targets a single organization with trusted host administrators; it does not claim strong isolation between hostile organizations.

## Technology Stack

| Layer | Technology / Approach |
| --- | --- |
| Frontend | React + TypeScript |
| Frontend tooling | Vite |
| UI components | shadcn/ui |
| Styling | Tailwind CSS |
| Backend API | Python + FastAPI |
| Request and response validation | Pydantic |
| Database | PostgreSQL |
| ORM | SQLAlchemy |
| Database migrations | Alembic |
| Background operations | Python worker with durable operation records in PostgreSQL |
| Container management | Docker SDK for Python, controlling the local Docker daemon |
| Agent runtime | OpenClaw with a pinned image, one container and private state per agent |
| Integration gateway | Separate Python/FastAPI process enforcing permissions and holding provider credentials |
| Model access | Python gateway enforcing approved models and spending limits |
| API transport | REST/JSON |
| Chat and activity streaming | Server-Sent Events (SSE) |
| User authentication | Deferred for the local prototype |
| Agent authentication | Per-agent gateway credentials from the beginning |
| Installation | Docker Compose on one Linux machine |
| Frontend serving | Vite production build served by FastAPI initially |
| Backend tests | pytest |
| Infrastructure tests | Integration tests against actual Docker containers |

Node.js is required only for frontend tooling. The platform backend, gateway, and host management will use Python. OpenClaw retains its own runtime dependencies inside its container.

## Application Structure

The platform will use one repository with separate processes for the API, Docker worker, and integration gateway. These components may share Python models and contracts while keeping their operational privileges separate.

- **API:** receives dashboard and chat requests, validates input, and records requested operations.
- **Worker:** processes durable operations, manages local Docker resources, and records observed agent state. Long-running operations such as image pulls happen outside HTTP request handlers.
- **Gateway:** authenticates agents, checks current permissions and budgets, and performs approved provider calls without exposing provider credentials to agents.
- **PostgreSQL:** stores agents, profiles, grants, operation progress, spending records, and audit metadata.
- **Agent containers:** run isolated OpenClaw instances with private state and constrained access to platform services.

## Planned Repository Layout

```text
frontend/       React application
backend/        API, database models, and shared Python code
worker/         Docker lifecycle and background operations
gateway/        Credentials, integrations, policies, and budgets
deploy/         Docker Compose and installation configuration
tests/          Focused backend and runtime checks
```

## Run locally

Requires Docker Engine with Compose on Linux, or Docker Desktop for development. No provider credentials are needed.

```sh
cp .env.example .env
docker compose up --build -d
```

Open http://127.0.0.1:8000. PostgreSQL is private; the API is published on loopback only. The migration service must finish before the API, worker, and gateway start. The status screen identifies the worker and gateway as scaffolds until their later implementation steps.

```sh
docker compose ps
docker compose logs api worker gateway
docker compose down
```

`down` preserves named volumes. `down --volumes` deletes the local database and worker state; use it only for disposable installations. The worker has privileged Docker-daemon access. Do not expose this unauthenticated prototype to other users.

## Development checks

Use Python 3.13, uv 0.11.21, Node 22.13 or later, and pnpm 11.19.0.

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

For frontend hot reload, run `pnpm --dir frontend dev` while Compose is running. Vite proxies API requests to loopback using `TALOS_PORT` from the root `.env` file or environment (8000 by default). There are no browser or end-to-end test dependencies; UI verification is manual.

## OpenClaw protocol check

The pinned release and architecture digests are recorded in `deploy/runtimes/openclaw.json`. The Python driver handles signed device pairing, messages, history, events, and cancellation. The model route is a deterministic diagnostic fixture and never calls a real provider. The default gateway rejects all tokens until workload identity is configured by the lifecycle worker.

The focused check runs two actual private OpenClaw containers and cleans its own resources. It requires Docker disk capacity for the runtime image and state volumes.

```sh
docker build --target verification -f deploy/Dockerfile -t talos-verification:local .
docker pull "$(python3 -c 'import json; print(json.load(open("deploy/runtimes/openclaw.json"))["image"])')"
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock talos-verification:local
```

Only this trusted verification runner gets the Docker socket; employee runtimes do not. The check uses normal named volumes by default. A test-only `TALOS_PROOF_RAM_VOLUMES=1` option is available for constrained development machines, but does not establish disk or daemon-restart persistence.

## Durable agent API

`POST /api/v1/agents` accepts `display_name` and `employee_label`. Create, start, stop, and delete requests require an `Idempotency-Key` header and return a durable operation with HTTP 202. Read its status through `GET /api/v1/operations/{id}`. Replaying the same key returns the original operation; changing its request or submitting conflicting work returns HTTP 409.

In this API-only layer, operations stay queued until the lifecycle worker is implemented. Queued means accepted, not running. Agent and operation records survive API process restarts. A fresh start request on an already ready agent is rejected; stop it first.

PostgreSQL checks use a random schema and remove only that schema afterward:

```sh
TALOS_TEST_DATABASE_URL=postgresql+psycopg://talos:password@127.0.0.1:5432/talos uv run pytest tests/integration/test_agents.py
```
