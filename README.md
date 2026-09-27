# Talos

A self-hosted control plane for personal employee agents. The goal is to let companies manage agent integrations, permissions, credentials, spending, and offboarding.

**Current status: local prototype.** Talos creates and manages native OpenClaw and Hermes instances with their own UI, tools, configuration, and persistent workspace. New agents default to native mode; a model provider is optional at creation. The existing Talos-managed conversation mode remains available with its simulator and opt-in OpenRouter gateway. User authentication, employee access, business permissions, and budgets are future work.

## What works

- Create, inspect, start, stop, and delete an agent with a display name and employee label.
- Choose OpenClaw or Hermes with the runtime icon picker; each agent has its own container, private internal network, and private state volume.
- Native UI access through a loopback-only TCP relay: OpenClaw device pairing or Hermes password login.
- Native tools and key-free search; choose an OpenRouter model in Talos or leave provider setup to the runtime.
- Durable PostgreSQL operations, idempotent requests, bounded retries, and adoption of owned Docker resources after a worker crash.
- Signed OpenClaw device pairing, diagnostic messages, ordered events, history support in the driver, and cancellation.
- Per-incarnation gateway credentials, checked against the current agent state and revocation/expiry on each fake-model request. Database failures deny access.
- Diagnostic delivery uncertainty is retained explicitly; messages are never automatically resent after a lost acknowledgment.

This prototype is for one organization with trusted host administrators. Keep the unauthenticated dashboard/API local. Agent containers have no Docker socket; only the trusted worker controls Docker. Native UI relays publish a random host-loopback port, and native agents use a shared outbound HTTP proxy. Managed agents retain their existing closed network. Containers share a host kernel, and Foundation does not establish the later enterprise permission or hostile-tenant isolation guarantees.

## Stack and layout

| Component | Implementation |
| --- | --- |
| Dashboard | React, TypeScript, Vite, shadcn/ui, Tailwind CSS; REST polling |
| API | Python 3.13, FastAPI, Pydantic; serves the frontend production build |
| Persistence | PostgreSQL 17, SQLAlchemy, Alembic |
| Worker | Python and Docker SDK; one worker per installation |
| Gateway | Separate FastAPI process; authenticated simulator and OpenRouter text inference |
| Runtimes | OpenClaw 2026.9.6 (Gateway v4) and Hermes 0.21.5, pinned by image digest |
| Packaging | uv, pnpm, Docker Compose |

`frontend/` contains the dashboard; `backend/` owns the API and database models; `worker/` owns Docker lifecycle and native runtime access; `gateway/` owns workload authentication and the fake model. `compose.yaml` starts the platform, `deploy/` contains its image build and runtime pin, and `tests/` contains focused backend and runtime checks. Node.js is used for frontend tooling; each native image carries its own runtime dependencies.

## Run locally

Use Docker Engine with Compose on Linux, or Docker Desktop for development, plus Python 3 to read the runtime pin. Allow disk space for the pinned runtime image and persistent volumes; each agent has a 2 GiB memory limit. Run these commands from the repository root:

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

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). PostgreSQL and the gateway are not published; the API binds to host loopback. The migration service must complete successfully before the application services start. The worker uses the pinned upstream image for managed agents and the locally built native image for native agents. Each native incarnation records and launches the immutable local image ID. Both runtimes are installed by the build command above. Rebuild the corresponding target (`native-runtime` for OpenClaw, `hermes-runtime` for Hermes) when its pin or dependencies change; stop/start applies the new image.

For a remote Linux development host, keep that binding and use a tunnel:

```sh
ssh -N -L 8000:127.0.0.1:8000 user@your-host
```

In the dashboard, click **New agent**, choose **OpenClaw** or **Hermes**, wait for **Stopped**, start it, and wait for **Ready**. Open the agent’s **Settings** tab and click **Open OpenClaw** or **Open Hermes** to open its native interface. For Hermes, choose a dashboard password of at least 12 characters when creating the agent, then sign in as `talos`. Configure your chosen model provider there; OpenRouter is optional. The instance starts without provider credentials, but an agent conversation still needs a usable model. Native provider credentials belong to that instance's private state. Alternatively, select OpenRouter in Talos and configure the shared key in Settings. After configuring a provider, use the agent’s Chat tab to converse directly in Talos; Settings retains access to the full native workspace.

The OpenClaw UI link expires after ten minutes and is single-use. After pairing, OpenClaw stores a device credential in your browser; revoke devices in OpenClaw when necessary. Do not share pairing links. Stop/start retains configuration, workspace, channel credentials, and paired devices while replacing the container and its gateway token. Keep the seeded gateway token and allowed-origin environment references when editing OpenClaw's gateway configuration.

Hermes uses its upstream password authentication and host-scoped session cookies. Each agent gets a distinct `<agent-id>.localhost` hostname so two Hermes instances can be used in the same browser. Keep the chosen password in your password manager. Talos stores a salted scrypt hash, never the plaintext password. Hermes owns subsequent native configuration changes; Talos has no password-reset flow. Stop/start preserves the private data volume and invalidates the previous dashboard session, so sign in again. The bundled s6 supervisor manages the dashboard and native gateway services; no model key is required to open setup.

On a remote Docker host, the UI also needs a tunnel for its assigned port. Obtain it with `docker port <native-container-name>-ui 18789/tcp`, then add `ssh -N -L <port>:127.0.0.1:<port> user@your-host` before opening the native UI. For Hermes, open the generated `.localhost` URL after tunneling; modern browsers resolve it to loopback. The port changes on stop/start. The Talos tunnel alone does not forward this separate UI port.

For the existing diagnostic/conversation flow, choose **OpenClaw** and check **Use Talos-managed conversations** when creating an agent. Hermes supports native mode only. Send a short synthetic message; include `[slow]` with the simulator to exercise cancellation. Existing agents keep managed mode. A start request on an already-ready agent is rejected: stop it first.

### Native tools and connectivity

The OpenClaw native image extends the pinned upstream image with `@openclaw/parallel-plugin@2026.9.6`, installed from its lockfile. The `parallel-free` search provider requires no API key; external service availability and limits still apply. Tool calls through a paid model can still cost money. `web_fetch` uses the outbound proxy, and shell/file tools run as the unprivileged container user against the private workspace.

Hermes uses the pinned official image unchanged, with its bundled Parallel key-free search selected at first boot. Search still depends on the external service's availability and limits. Native shell and file tools run as UID 10000 in `/opt/data/workspace`; `/opt/data` persists across starts. No OpenRouter account is required to provision either runtime.

Telegram, Slack, and other integrations use the selected runtime's own plugins and account setup. They are not preconnected. Install/configure the required native plugin and supply your own account credentials. HTTP(S) clients must honor the supplied proxy variables or their integration's explicit proxy setting; raw TCP/UDP, inbound webhooks, LAN services, and tools requiring host privileges are not enabled by this setup. Telegram polling and Slack Socket Mode avoid public inbound ports, but channel-specific proxy support and credentials must be checked during setup. No external messages are sent by Talos provisioning.

The browser tool keeps upstream security defaults. The OpenClaw image does not install Chromium; its browser automation needs a compatible sandboxed browser configured separately. Hermes includes its upstream browser dependencies. Browser automation and authenticated channel delivery have not been verified by Talos provisioning checks. Host package installation and Docker-backed nested sandboxes are unavailable. Tools may install user-space dependencies into writable state where their installers support it.

Native instances keep internal Docker bridges. A shared Squid proxy allows public HTTP on port 80 and HTTPS CONNECT on port 443, denying private, loopback, link-local, and reserved destinations after DNS resolution. A separate small TCP relay per instance forwards only to that instance's UI and publishes only on `127.0.0.1`. Neither the relay nor the proxy mounts agent state or credentials. These are container/network boundaries for a trusted local administrator, not enterprise tenant isolation or protection against all prompt injection. External content can influence an agent with full native tools and its configured credentials.

## Operations and diagnostics

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

Run `pnpm --dir frontend dev` for hot reload while Compose is running. Vite binds to loopback and uses `TALOS_PORT` from the root `.env` file or environment for API requests (8000 by default). UI verification is manual. There are no GitHub Actions workflows, browser-test dependencies, or end-to-end test suites.

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

## OpenRouter and the model picker

**Native agents:** Create an agent shows **Handled by agent** by default. This leaves
provider/model setup in Hermes or OpenClaw; a fresh native workspace needs a provider
before it can answer. Choose **OpenRouter** and a catalog model at creation or in the
agent's Settings to use the installation key described below. Settings shows whether
the key is configured; selecting a model does not create credentials.

Native model changes use the existing durable operation worker and can be saved while
the agent runs. OpenRouter changes affect the next provider request; requests already
sent retain their captured model. Native sessions with explicit model overrides may
need a new session. Talos Test agent selects the saved model on every turn. Runtimes
started before this feature need one stop/start to add the internal gateway proxy
exclusion. Returning to **Handled by agent** restores the prior native model settings.
Role permissions, other provider credentials, workspace and history are preserved.

The shared OpenRouter key is read by the gateway, never copied into agents. Native agents receive an incarnation
token for `/native/v1/chat/completions`; this route preserves tool calls and results,
and accepts only active native agents with an OpenRouter selection. Native runtimes
still own tool dispatch and generation settings. Native inference usage accounting is
not added here. The managed text-only route retains its no-tools contract.

`POST /api/v1/agents` accepts optional `model_id`. For a native agent,
`POST /api/v1/inference/agents/{id}/native` with `{ "model_id": "lab/model" }` (or
JSON `null` to hand control back) requires `Idempotency-Key` and returns HTTP 202.
Poll the returned operation; failed operations do not claim the selection was applied.

**Talos-managed conversations:**

The workspace **Settings** page selects the default model for new messages without
restarting agents. Each admitted message stores its model choice, so queued and
running requests keep that model even when the selection changes. The simulator
remains an explicit option and is the initial setting; missing credentials never
silently fall back to it. Model settings and run snapshots persist in PostgreSQL.

To enable external inference:

1. Open **Settings → OpenRouter API key**, paste your key, and click **Verify & save key**.
   Talos verifies it with OpenRouter's key endpoint without making a model request.
   Invalid keys and verification failures leave the previous key unchanged.
2. For a native agent, choose **OpenRouter** and a model during creation or in the
   agent's **Settings**. **Handled by agent** continues to use the native provider setup.
3. For Talos-managed conversations, choose a model in **Settings → Default model**
   and click **Save settings**, or customize the model for one agent. Compatible
   text models come from OpenRouter's public catalog.

The password field is never prefilled. **Replace key** and **Remove key** affect all
agents using OpenRouter through Talos, starting with their next provider request;
requests already sent can finish with the previous key. No restart is needed.
The app stores the key in the persistent `provider-secrets` Docker volume, as a
mode-0600 file inside a mode-0700 directory owned by UID 10001. The API writes it;
the gateway mounts the volume read-only. Workers and agents do not mount it.
This is a private file, not encrypted storage: protect the Docker host and backups.
The key is never returned by the API, included in agent configuration, or stored
in browser local storage. Keep the existing local/tunneled administrator deployment.

Alternatively, deployment operators can mount an existing private key file using
`TALOS_OPENROUTER_SECRET_FILE` and
`docker compose -f compose.yaml -f compose.openrouter.yaml up --build -d`.
The file must be readable by gateway UID 10001. This deployment secret takes
precedence, and the app shows it as deployment-managed and disables edits, even
if the mounted key is invalid. Keep using both Compose files for that installation.
To rotate a deployment secret, replace the file and recreate the gateway as needed
for the bind mount to see it. Never put the key value in `.env` or the command line.

The **Agents** page contains the agent list and each agent's **Conversation** and
**Settings** views. Agent settings inherit the workspace configuration by default.
Choose **Customize for this agent**, select a model and optional generation settings,
then **Save settings** to store a complete override for that agent. Workspace changes
will not change this override. **Use workspace defaults** removes it and follows the
latest shared configuration again. Run admission resolves the configuration under
the agent lock and snapshots its source, model, capabilities and parameters, so
changing or clearing an override cannot reroute a queued or running message.

The lab picker groups the live catalog by model author and shows locally bundled
[Lobe Icons](https://github.com/lobehub/lobe-icons) logos. Hover labels and accessible
names identify each lab; unrecognized labs use initials and remain selectable.
Choosing a lab filters the model list without changing the active selection until
**Save settings** is clicked.

The gateway has an outbound network in the default Compose configuration. Managed
agents remain on private internal networks with revocable Talos credentials; native
agents use their separate proxy for other outbound traffic. The public model catalog
is fetched by the API without a provider credential.

Real inference uses the admitted run's server-owned model, capability and settings
snapshot and a fixed HTTPS OpenRouter endpoint. By default Talos omits reasoning,
output-token and sampling parameters: the provider chooses its defaults and still
enforces its own limits. **Advanced settings** allows explicit reasoning effort,
maximum output tokens (including reasoning), temperature and top P where supported.
The API checks the current catalog, rejects disabling mandatory reasoning and
requests a provider that supports all explicit settings. Switching models resets
unsaved overrides. Save settings once after upgrading an existing installation
to load its selected model's capabilities.

Before sending a new run, the trusted worker atomically publishes the snapshotted
model profile into the existing read-only runtime config volume. It waits for
OpenClaw's hot-reloaded model catalog, then selects that profile for the session.
Context and output capacity come from the catalog, not the fixture's metadata.
Only model configuration changes; runtime security and launch ownership labels
remain unchanged. The gateway ignores runtime-supplied generation parameters and
uses only the admitted operator settings. Tool, browser and plugin access remains
disabled. Managed aliases (`default`, `talos-…`) resolve to the admitted model;
`fixture` retains the offline simulator.

Each provider call records reported input, output and reasoning tokens, cost in
USD, duration and finish reason. Missing usage or cost stays unavailable rather
than zero, including cancelled requests without final accounting. A provider
`length` finish is shown as **Output limit reached**. Calls within the same run
are recorded separately; these records are observability, not a billing ledger.

Operational safeguards are separate from generation settings. Compose exposes
`TALOS_INFERENCE_TIMEOUT_SECONDS` (1800),
`TALOS_INFERENCE_IDLE_TIMEOUT_SECONDS` (300),
`TALOS_INFERENCE_MAX_OUTPUT_CHARS` (4000000, including reasoning transport data),
and `TALOS_INFERENCE_MAX_REQUEST_BYTES` (16777216). The runtime and worker deadlines
include a small grace period after the gateway deadline. These generous defaults
bound stalled requests and memory/storage use; there is no default Talos token
budget and no 500-event cutoff. OpenClaw and the provider retain their own protocol
and context limits. Explicit timeout and resource-limit failures are recorded.

The gateway checks admission, expiry, cancellation and revocation during requests,
including while waiting for a silent provider. Disconnects close the upstream
connection. Streaming errors and truncated responses fail explicitly; the gateway
does not retry potentially billed requests. Provider-side cancellation and billing
vary, so cancelling cannot undo charges already incurred.

The picker is part of the existing **local-only, unauthenticated** operator UI.
Do not expose it publicly; employee accounts and authorization remain future work.
Subscription sharing, spending budgets, and API fallback are not included. The protocol proof covers the real pinned OpenClaw container with a
controlled upstream transport, including model changes, provider failures and
cancellation. To opt into one paid DeepSeek V4 Flash check as well, run
the verifier with your key mounted read-only (never pass the key value on the
command line):

```sh
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  --mount type=bind,source=/absolute/private/path/openrouter.key,target=/run/secrets/live_openrouter,readonly \
  --env TALOS_PROOF_OPENROUTER_KEY_FILE=/run/secrets/live_openrouter \
  talos-verification:local
```

The live check uses the pinned OpenClaw container and the gateway transport; it
makes a short request with provider defaults (and therefore no fixed token budget). Normal tests use no provider key.

## Stop, back up, and update

**Stop or delete agents through Talos first and wait for their operations.** Then use `docker compose down` to stop the platform. Dynamically created agent containers are not Compose services: `down` alone does not stop them. Normal `down` preserves named volumes.

`docker compose down --volumes` destroys the platform database and worker credentials, but does not clean up dynamically created agent volumes. Do not use it to uninstall an installation with agents still present; delete agents through Talos while the database and worker are available. Avoid broad Docker prune commands.

Backups are manual. After stopping agents, stop API/worker/gateway writes, then take a consistent PostgreSQL backup and volume snapshots. Preserve the `worker-state` volume, each agent's state/config volumes, `.env`, the checked-out commit, and runtime digest alongside the database. Default platform volume names are `talos_postgres-data` and `talos_worker-state`; dynamic agent resources carry `io.talos.project`, `io.talos.installation`, and `io.talos.agent` labels. Protect these backups: worker-state and configuration volumes contain workload credentials. A database dump alone cannot restore the installation.

For updates, retain those backups, keep the Compose project and installation identifiers unchanged, check out a reviewed commit, pull its approved runtime digest, and rerun `docker compose up --build -d`. Migrations run before services start. Do not edit runtime pins without validating the corresponding lifecycle, authentication, and driver contracts. Automated rollback, runtime upgrades, and backup/restore orchestration are not implemented; restore the matching database, volumes, and code together if rollback is needed.

## Troubleshooting

```sh
docker compose ps -a
docker compose logs migrate api worker gateway
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

The worker restarts automatically after a process failure; an explicit Compose stop keeps it stopped. Run only one worker per installation and preserve its private state. A lock prevents workers sharing that state directory from running concurrently; it does not coordinate separate worker volumes. Dashboard worker/gateway capability labels are not live heartbeats—use operation results and service logs.

### Runtime icon attribution

The picker uses the upstream [OpenClaw favicon](https://github.com/openclaw/openclaw/blob/eb377ac59e6c9fd6c7705028034812becf00271b/ui/public/favicon.svg) and [Hermes icon](https://github.com/NousResearch/hermes-agent/blob/4b7229d612324adcf86ede7c181df542a1697fd6/assets/icon-master.svg), distributed under their repositories’ MIT licenses. The names and marks identify the selected runtime.


## Employee and role administration

The local administrator API supports `/api/v1/employees` and `/api/v1/roles`
(GET/POST), their `/{id}` resources (PUT/DELETE), and GET `/api/v1/capabilities`.
Employees have one role; roles select native capability groups. Referenced records
cannot be deleted. These are administrator records, not employee login accounts.

Create an agent with `employee_id`, or attach a stopped agent with
`PUT /api/v1/agents/{id}/employee`. Legacy `employee_label` requests remain supported;
existing labels are never automatically converted into employee identities.
No running agent is changed by saving a role; apply it explicitly or start the agent.

### Applying role permissions

For assigned native agents, Start captures the current role and applies it before
starting the runtime. Saving a role or changing an employee's role leaves running
agents unchanged. `POST /api/v1/agents/{id}/apply-role` accepts an `Idempotency-Key`,
returns HTTP 202, and uses the existing operation polling endpoint. It interrupts
running work, applies the captured revision while stopped, and restarts only when
previously running. A newer edit remains pending. Failed applications leave the
agent stopped; inspect the operation error and native configuration before retrying.

Agent responses expose `role` (saved), `applied_role` (last successful snapshot),
and `permissions_pending`. Managed conversation agents keep their no-tools
contract; unassigned native agents keep their existing native configuration.

Roles govern native tool availability and dispatch, not arbitrary-code containment.
Terminal execution can access files and the network even when dedicated tools are
disabled. Native configuration is a trusted-administrator surface: direct changes
outside Talos are outside this contract. Provider setup, credentials, workspace,
and conversation history remain in the existing native settings and state volume.
