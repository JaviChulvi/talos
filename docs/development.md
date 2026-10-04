# Development and validation

[← Talos](../README.md) · [Documentation index](../README.md#documentation)

Build Talos from source, run local checks, and inspect runtime acceptance evidence. All shell commands in this guide run from the repository root. For a promoted release bundle, use [Installation and recovery](installation.md) instead.

## Stack and layout

| Component | Implementation |
| --- | --- |
| Dashboard | React, TypeScript, Vite, shadcn/ui, Tailwind CSS; REST polling |
| API | Python 3.13, FastAPI, Pydantic; serves the frontend production build |
| Persistence | PostgreSQL 17, SQLAlchemy, Alembic |
| Worker | Python and Docker SDK; one worker per installation |
| Connector | Telegram long polling and Slack Socket Mode; durable inbox/outbox |
| Gateway | Separate FastAPI process; simulator, managed text inference, and native OpenRouter inference |
| Runtimes | OpenClaw 2026.9.6 (Gateway v4) and Hermes 0.21.5, pinned by image digest |
| Packaging | uv, pnpm, Docker Compose |

`frontend/` contains the dashboard; `backend/` owns the API and database models; `worker/` owns Docker lifecycle and native runtime access; `connector/` owns employee channel admission and delivery; `gateway/` owns workload authentication and model routing. `compose.yaml` starts the platform, `deploy/` contains its image build and runtime pin, and `tests/` contains focused backend and runtime checks. See [runtime versions and connectivity](runtimes.md) for native image behavior. Node.js is used for frontend tooling; each native image carries its own runtime dependencies.

## Run locally

Use Docker Engine with Compose on Linux, or Docker Desktop for development, plus Python 3 to read the runtime pin. Allow disk space for the pinned runtime image and persistent volumes; each agent has a 2 GiB memory limit. Obtain the private repository with `git clone git@github.com:JaviChulvi/talos.git`, then `cd talos`. Run these commands from the repository root:

```sh
cp .env.example .env
```

Edit the example database password in `.env` **before the first startup**. `POSTGRES_PASSWORD` initializes a new database; editing `.env` later does not change an existing database's password. For an initialized database, rotate the PostgreSQL role password and update `.env` together.

```sh
docker pull "$(python3 -c 'import json; print(json.load(open("deploy/runtimes/openclaw.json"))["image"])')"
docker compose build native-runtime hermes-runtime
docker compose up --build -d
docker compose ps
```

Create the administrator with `docker compose exec api python -m backend.app.auth bootstrap`, then open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with that password. PostgreSQL and the gateway are not published; the API binds to host loopback. The migration service must complete successfully before the application services start. The worker uses the pinned upstream image for managed agents and the selected locally installed wrapper image for native agents. Each native incarnation records and launches the immutable local image ID. Both bundled runtimes are installed by the build command above. Rebuilding a native image affects new agents; existing initialized agents keep their saved image across stop/start.

For a remote Linux development host, keep that binding and use a tunnel:

```sh
ssh -N -L 8000:127.0.0.1:8000 user@your-host
```

In the dashboard, click **New agent**, choose **OpenClaw** or **Hermes**, wait for **Stopped**, start it, and wait for **Ready**. Open the agent’s **Settings** tab and click **Open OpenClaw** or **Open Hermes** to open its native interface. For Hermes, choose a dashboard password of at least 12 characters when creating the agent, then sign in as `talos`. Configure your chosen model provider there; OpenRouter is optional. The instance starts without provider credentials, but an agent conversation still needs a usable model. Native provider credentials belong to that instance's private state. Alternatively, select OpenRouter in Talos and configure the shared key in Settings. After configuring a provider, use the agent’s Chat tab to converse directly in Talos; Settings retains access to the full native workspace.

The OpenClaw UI link expires after ten minutes and is single-use. After pairing, OpenClaw stores a device credential in your browser; revoke devices in OpenClaw when necessary. Do not share pairing links. Stop/start retains configuration, workspace, channel credentials, and paired devices while replacing the container and its gateway token. Keep the seeded gateway token and allowed-origin environment references when editing OpenClaw's gateway configuration.

Hermes uses its upstream password authentication and host-scoped session cookies. Each agent gets a distinct `<agent-id>.localhost` hostname so two Hermes instances can be used in the same browser. Keep the chosen password in your password manager. Talos stores a salted scrypt hash, never the plaintext password. Hermes owns subsequent native configuration changes; Talos has no Hermes password-reset flow. Stop/start preserves the private data volume and invalidates the previous dashboard session, so sign in again. The bundled s6 supervisor manages the dashboard and native gateway services; no model key is required to open setup.

On a remote Docker host, the UI also needs a tunnel for its assigned port. Obtain it with `docker port <native-container-name>-ui 18789/tcp`, then add `ssh -N -L <port>:127.0.0.1:<port> user@your-host` before opening the native UI. For Hermes, open the generated `.localhost` URL after tunneling; modern browsers resolve it to loopback. The port changes on stop/start. The Talos tunnel alone does not forward this separate UI port.

For the existing diagnostic/conversation flow, choose **OpenClaw** and check **Use Talos-managed conversations** when creating an agent. Hermes supports native mode only. Send a short synthetic message; include `[slow]` with the simulator to exercise cancellation. Existing agents keep managed mode. A start request on an already-ready agent is rejected: stop it first.

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
pnpm --dir frontend test
pnpm --dir frontend build
```

Run `pnpm --dir frontend dev` for hot reload while Compose is running. Vite binds to loopback and uses `TALOS_PORT` from the root `.env` file or environment for API requests (8000 by default). Browser verification uses the existing browser tooling; no browser-test framework is installed. Administrator API restart checks are in `tests/integration/test_admin_auth_process.py` and use real API subprocesses with a disposable PostgreSQL schema. The repository has manually dispatched [release candidate](../.github/workflows/release-candidate.yaml), [installation acceptance](../.github/workflows/installation-acceptance.yml), and [release promotion](../.github/workflows/promote-release.yml) workflows. The [Checks workflow](../.github/workflows/checks.yml) runs Python, frontend, and redacted secret checks on pull requests with read-only permissions and no production secrets. Run the local checks above before submitting changes.

The focused OpenClaw protocol check uses two real containers, synthetic model responses, and normal named volumes by default. Its trusted runner needs Docker access and removes its own runtime resources:

```sh
docker build --target verification -f deploy/Dockerfile -t talos-verification:local .
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock talos-verification:local
```

### Repeatable native runtime reliability proof

From the repository root, with Docker running:

```sh
uv run python -m tests.reliability
```

This builds the current verification image and both native test images from the
repository's pinned upstream digests. It uses an isolated internal network,
disposable PostgreSQL, RAM-backed agent volumes, fake credentials, and local
model/channel fixtures. It does not require employee recipients or provider keys.
The trusted test runner mounts Docker's socket; agent containers do not.
The runner removes its containers, volumes and network and writes `results.xml`,
`pytest.log`, and `environment.json` under `.data/reliability/<run-id>/`.
The environment records the tested image IDs, source revision, and dirty status.
Run-specific build records capture immutable image IDs directly, preventing
concurrent runs from exchanging images. The runner retries subnet collisions
during initial network allocation.

| Scenario | Expected behavior and evidence |
| --- | --- |
| Worker killed before acknowledgment, during a tool, or before result commit | Durable send intent becomes `unknown`; a fixture's independent action journal contains exactly one action; Telegram/Slack redelivery reuses the original turn without sending again. |
| Connector killed after its provider accepts a response | The outbox becomes `uncertain`; restart does not resend the response. |
| Employee access revoked, channel disabled, credentials rotated, identity reassigned, or verification withdrawn during work | The worker persists cancellation and requests native abort; unauthorized channel delivery is blocked. Rejected cancellation retains `unknown`. |
| Native restart and tool denial | Both pinned native containers retain separate channel histories. OpenClaw also rejects a denied native read invocation and closes a synthetic provider stream when each channel's access is revoked. |
| Runtime version selected, default tag rebuilt, or catalog changed | Creation saves one concrete release; replay recovers the original selection. Restart keeps the initialized image ID; unapproved versions and mismatched image release labels are rejected. Saved setup artifacts remain readable after catalog removal. |
| Budget exhausted or accounting unavailable | Existing gateway tests reject new paid calls before provider dispatch. This is admission against known spending; already admitted/concurrent calls can overshoot, and missing costs remain explicit. |
| Incompatible setup or unsupported receipt schema | Preflight or inspection fails before changing managed state. New receipts pin their schema version, runtime image ID, architecture, labels, and applied policy. Original unversioned receipts remain readable and are versioned on explicit Apply. |
| Setup upgraded or helper process exits during publication | Explicitly selecting the previous immutable setup restores its skills, instructions and policy. Tests compare private fixture history and a SQLite memory file byte for byte across rollback. |

Employee access is exercised through Telegram and Slack admission/delivery. The
dashboard is an administrator/testing surface. Revocation is observed by the
worker between runtime events (normally within its 250 ms event wait); native
cancellation cannot retract completed external actions. Process-kill tests use a
real WebSocket adapter and PostgreSQL with a synthetic runtime/action; the native
acceptance tests use the real pinned native containers with synthetic inference
and channel providers. Passing these cases establishes those contracts only.

The next reliability backlog is deliberately narrower than a universal security claim:

| Workstream | Remaining acceptance criterion |
| --- | --- |
| Recovery | For each supported business-action tool, retain a durable action identity, prove provider-side idempotency or require reconciliation, and inject failure after provider acceptance but before the tool result. An uncertain run alone does not establish exactly-once business actions. |
| Permissions | Add an employee approval broker for sensitive channel actions with durable, argument-bound, expiring decisions; test revoked decisions before execution. Before offering employee website access, apply the same identity, admission, history and delivery contract to that adapter. Test cross-employee containers with real granted file and network tools. |
| Version compatibility | Pin hosted MCP input/output schemas and reject shape changes even when tool names are unchanged. Local connector payloads and skill assets are content-hashed today; hosted schemas can still drift. |
| Runtime/memory upgrades | Execute the release installer's stopped-runtime backup/restore acceptance on supported hosts, then validate a real old/new runtime image pair, native memory schema migration failure, and downgrade. This suite proves setup rollback under one runtime image, not safe rollback after arbitrary upstream memory migrations. Restore code, PostgreSQL, artifacts, secrets and runtime state together. |

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
