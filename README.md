# Talos

A self-hosted control plane for personal employee agents. The goal is to let companies manage agent integrations, permissions, credentials, spending, and offboarding.

**Current status: local prototype.** Talos creates and manages native OpenClaw and Hermes instances with their own UI, tools, configuration, and persistent workspace. New agents default to native mode; a model provider is optional at creation. The existing Talos-managed conversation mode remains available with its simulator and opt-in OpenRouter gateway. Employee roles, Talos-routed spend reporting, and employee monthly allowances are available. A single built-in administrator secures the dashboard and management API, with host-only setup and recovery. Employees access their assigned agents through approved private Telegram or Slack identities.

## What works

- Create, inspect, start, stop, and delete an agent with a display name and employee label.
- Choose OpenClaw or Hermes with the runtime icon picker; each agent has its own container, private internal network, and private state volume.
- Native UI access through a loopback-only TCP relay: OpenClaw device pairing or Hermes password login.
- Native tools and key-free search; choose an OpenRouter model in Talos or leave provider setup to the runtime.
- Durable PostgreSQL operations, idempotent requests, bounded retries, and adoption of owned Docker resources after a worker crash.
- Signed OpenClaw device pairing, diagnostic messages, ordered events, history support in the driver, and cancellation.
- Per-incarnation gateway credentials, checked against the current agent state and revocation/expiry on each fake-model request. Database failures deny access.
- Diagnostic delivery uncertainty is retained explicitly; messages are never automatically resent after a lost acknowledgment.

This prototype is for one organization with trusted host administrators. The dashboard/API binds to loopback by default; shared-host access uses an explicitly configured HTTPS reverse proxy and administrator login. Agent containers have no Docker socket; only the trusted worker controls Docker. Native UI relays publish a random host-loopback port, and native agents use a shared outbound HTTP proxy. Managed agents retain their existing closed network. Containers share a host kernel, and Foundation does not establish the later enterprise permission or hostile-tenant isolation guarantees.

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

Create the administrator with `docker compose exec api python -m backend.app.auth bootstrap`, then open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with that password. PostgreSQL and the gateway are not published; the API binds to host loopback. The migration service must complete successfully before the application services start. The worker uses the pinned upstream image for managed agents and the locally built native image for native agents. Each native incarnation records and launches the immutable local image ID. Both runtimes are installed by the build command above. Rebuild the corresponding target (`native-runtime` for OpenClaw, `hermes-runtime` for Hermes) when its pin or dependencies change; stop/start applies the new image.

For a remote Linux development host, keep that binding and use a tunnel:

```sh
ssh -N -L 8000:127.0.0.1:8000 user@your-host
```

In the dashboard, click **New agent**, choose **OpenClaw** or **Hermes**, wait for **Stopped**, start it, and wait for **Ready**. Open the agent’s **Settings** tab and click **Open OpenClaw** or **Open Hermes** to open its native interface. For Hermes, choose a dashboard password of at least 12 characters when creating the agent, then sign in as `talos`. Configure your chosen model provider there; OpenRouter is optional. The instance starts without provider credentials, but an agent conversation still needs a usable model. Native provider credentials belong to that instance's private state. Alternatively, select OpenRouter in Talos and configure the shared key in Settings. After configuring a provider, use the agent’s Chat tab to converse directly in Talos; Settings retains access to the full native workspace.

The OpenClaw UI link expires after ten minutes and is single-use. After pairing, OpenClaw stores a device credential in your browser; revoke devices in OpenClaw when necessary. Do not share pairing links. Stop/start retains configuration, workspace, channel credentials, and paired devices while replacing the container and its gateway token. Keep the seeded gateway token and allowed-origin environment references when editing OpenClaw's gateway configuration.

Hermes uses its upstream password authentication and host-scoped session cookies. Each agent gets a distinct `<agent-id>.localhost` hostname so two Hermes instances can be used in the same browser. Keep the chosen password in your password manager. Talos stores a salted scrypt hash, never the plaintext password. Hermes owns subsequent native configuration changes; Talos has no Hermes password-reset flow. Stop/start preserves the private data volume and invalidates the previous dashboard session, so sign in again. The bundled s6 supervisor manages the dashboard and native gateway services; no model key is required to open setup.

On a remote Docker host, the UI also needs a tunnel for its assigned port. Obtain it with `docker port <native-container-name>-ui 18789/tcp`, then add `ssh -N -L <port>:127.0.0.1:<port> user@your-host` before opening the native UI. For Hermes, open the generated `.localhost` URL after tunneling; modern browsers resolve it to loopback. The port changes on stop/start. The Talos tunnel alone does not forward this separate UI port.

For the existing diagnostic/conversation flow, choose **OpenClaw** and check **Use Talos-managed conversations** when creating an agent. Hermes supports native mode only. Send a short synthetic message; include `[slow]` with the simulator to exercise cancellation. Existing agents keep managed mode. A start request on an already-ready agent is rejected: stop it first.

### Native tools and connectivity

The OpenClaw native image extends the pinned upstream image with `@openclaw/parallel-plugin@2026.9.6`, installed from its lockfile. The `parallel-free` search provider requires no API key; external service availability and limits still apply. Tool calls through a paid model can still cost money. `web_fetch` uses the outbound proxy, and shell/file tools run as the unprivileged container user against the private workspace.

Hermes extends the pinned official image with browser dependencies, with its bundled Parallel key-free search selected at first boot. Search still depends on the external service's availability and limits. Native shell and file tools run as UID 10000 in `/opt/data/workspace`; `/opt/data` persists across starts. No OpenRouter account is required to provision either runtime.

Telegram, Slack, and other integrations use the selected runtime's own plugins and account setup. They are not preconnected. Install/configure the required native plugin and supply your own account credentials. HTTP(S) clients must honor the supplied proxy variables or their integration's explicit proxy setting; raw TCP/UDP, inbound webhooks, LAN services, and tools requiring host privileges are not enabled by this setup. Telegram polling and Slack Socket Mode avoid public inbound ports, but channel-specific proxy support and credentials must be checked during setup. No external messages are sent by Talos provisioning.

Both native images include Chromium. Hermes also bundles pinned `agent-browser`
and `browser-use` CLIs so the first browser call does not install dependencies.
Browsers use Talos's public-only Squid proxy, including loopback destinations;
agents remain on internal Docker networks. The OpenClaw image carries a narrow
patch to skip local DNS preflight for explicitly proxy-routed browser profiles.
The proxy resolves names and denies private/metadata addresses and non-web ports.
Direct browser profiles retain upstream DNS validation.

Chromium runs without its nested process sandbox, as required by the existing
Docker seccomp/no-new-privileges policy; the unprivileged container, dropped
capabilities, read-only root filesystem, and restricted egress remain its boundary.
OpenClaw's seeded `browser.ssrfPolicy.dangerouslyAllowPrivateNetwork` is needed
to admit an explicit browser proxy; it does **not** grant private-network access
through Squid. Keep the proxy and `--proxy-bypass-list=<-loopback>` together.
Rebuild both native images to install the browser dependencies. Existing OpenClaw
configurations are user-owned and are not overwritten: copy the `browser` block
from `worker/runtime.py:native_config` into their native settings when opting in.
Host package installation and Docker-backed nested sandboxes remain unavailable.

Native instances keep internal Docker bridges. A shared Squid proxy allows public HTTP on port 80 and HTTPS CONNECT on port 443, denying private, loopback, link-local, and reserved destinations after DNS resolution. A separate small TCP relay per instance forwards only to that instance's UI and publishes only on `127.0.0.1`. Neither the relay nor the proxy mounts agent state or credentials. These are container/network boundaries for a trusted local administrator, not enterprise tenant isolation or protection against all prompt injection. External content can influence an agent with full native tools and its configured credentials.

## Administrator setup, login, and recovery

After migrations complete, create the single built-in `admin` from the Talos host:

```sh
docker compose exec api python -m backend.app.auth bootstrap
```

Both prompts hide input. Choose 15–128 characters; spaces and Unicode are preserved
exactly. The database stores a salted scrypt hash. Bootstrap never overwrites an
existing administrator. For recovery or ordinary password rotation:

```sh
docker compose exec api python -m backend.app.auth reset-password
```

Reset changes the password and revokes all administrator sessions in one transaction.
These commands require trusted host access; there is no web registration/reset endpoint.
All management API reads and mutations now require an administrator session. Before
bootstrap they remain locked. The dashboard shows setup instructions until bootstrap, then a password-only login form.
Sign out revokes the browser session and clears displayed/cached management data across
open tabs. Session status is checked every 15 seconds and when a tab becomes visible;
management 401 responses clear the view immediately.

The authentication API has three routes: `GET /api/v1/auth/session` returns only
`setup_required` and `authenticated`; `POST /api/v1/auth/login` accepts
`{"password":"…"}` and sets a cookie; `POST /api/v1/auth/logout` revokes the current
session and clears the cookie. Tokens are never returned in response bodies.
Every mutation, including login/logout, requires `X-Talos-Request: 1`. Login also
requires `Content-Type: application/json`. When present, `Origin` must exactly match
`TALOS_ALLOWED_ORIGINS`; no CORS or forwarded-header trust is enabled. Header-bearing
non-browser clients can omit Origin. ZIP imports retain their existing content type.

Sessions use random tokens stored only as hashes, an HttpOnly/SameSite=Strict cookie,
and a fixed eight-hour expiry that survives API restarts without renewal. Logout revokes
one browser session; password reset revokes every session. After five failed attempts,
login is blocked for 60 seconds, including across API restarts. Attempts are serialized
in PostgreSQL; successful login clears the counter. Password hashing uses the existing
cryptography implementation of scrypt (`N=131072,r=8,p=1`) with random salts.

The default cookie requires HTTPS. For local loopback HTTP, explicitly set
`TALOS_ADMIN_COOKIE_SECURE=false` as in `.env.example`. Compose configures exact
localhost/127.0.0.1 origins using `TALOS_PORT`. For Vite development, add
`http://127.0.0.1:5173` to `TALOS_ALLOWED_ORIGINS` explicitly. API docs are disabled.

For an HTTPS reverse proxy, keep the API on loopback, terminate TLS at the proxy, set
`TALOS_ADMIN_COOKIE_SECURE=true`, `TALOS_ALLOWED_HOSTS=["talos.example.com"]`, and
`TALOS_ALLOWED_ORIGINS=["https://talos.example.com"]`. Pass the public Host and browser
Origin unchanged. The API uses those configured values, never `X-Forwarded-*`, for
browser admission/cookie settings. Configure the proxy's request logging to exclude
Cookie/Set-Cookie and bodies. Native runtime UI relays retain their separate loopback
access and runtime authentication; administrator login does not publish those relays.

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

Run `pnpm --dir frontend dev` for hot reload while Compose is running. Vite binds to loopback and uses `TALOS_PORT` from the root `.env` file or environment for API requests (8000 by default). Browser verification uses the existing browser tooling; no browser-test framework is installed. Administrator API restart checks are in `tests/integration/test_admin_auth_process.py` and use real API subprocesses with a disposable PostgreSQL schema. There are no GitHub Actions workflows.

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
need a new session. Talos conversations select the saved model on every turn. Runtimes
started before this feature need one stop/start to add the internal gateway proxy
exclusion. Returning to **Handled by agent** restores the prior native model settings.
Role permissions, other provider credentials, workspace and history are preserved.

The shared OpenRouter key is read by the gateway, never copied into agents. Native agents receive an incarnation
token for `/native/v1/chat/completions`; this route preserves tool calls and results,
and accepts only active native agents with an OpenRouter selection. Native runtimes
still own tool dispatch and generation settings. Native inference usage is recorded in the durable call ledger, including calls
without a Talos chat run. Employee monthly allowances apply at gateway admission. The managed text-only route retains its no-tools contract.

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
are recorded separately; new calls are stored in the durable ledger; missing costs remain unknown.

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

The picker is part of the **administrator-authenticated** dashboard. Employees access
their assigned agents through approved Telegram/Slack identities. Native subscription
sharing and API fallback are not included. The protocol proof covers the real pinned OpenClaw container with a
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

For updates, retain those backups, keep the Compose project and installation identifiers unchanged, check out a reviewed commit, pull its approved runtime digest, and rerun `docker compose up --build -d`. Migrations run before services start. When upgrading an existing local HTTP installation,
add `TALOS_ADMIN_COOKIE_SECURE=false` to its `.env` explicitly, bootstrap the administrator
after migration, and sign in. Existing agent/employee data is retained; the first
administrator bootstrap does not alter it. Reset requires host access and never deletes
agents, credentials, conversations, or usage.

Do not edit runtime pins without validating the corresponding lifecycle, authentication, and driver contracts. Automated rollback, runtime upgrades, and backup/restore orchestration are not implemented; restore the matching database, volumes, and code together if rollback is needed.

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

Employee channel configuration is admin-only. `/api/v1/channels` configures one
Telegram bot and one Slack workspace app; channels are disabled initially. Store
write-only credentials with `PUT /api/v1/channels/{id}/credentials` using
`{"values":{"bot_token":"..."}}` for Telegram, or both `app_token` and `bot_token`
for Slack. These immutable credential versions cannot be assigned to roles or
agents, and rotation invalidates channel verification.

`/api/v1/employee-accesses` associates an employee's assigned native agent with a
stable platform user ID (and Slack workspace ID). New accesses are pending.
`POST /{id}/invitation` returns a single-use token valid for 15 minutes; claiming
it remains pending until the admin calls `POST /{id}/approve`. `POST /{id}/disable`
revokes access. No employee login or public Talos API is introduced. Messaging
transport and guided handoff use these records without employee Talos accounts.

The `connector` service receives private Telegram text messages by long polling.
Enable the configured channel after saving its bot token. It verifies `getMe`
and refuses a conflicting webhook or polling consumer. The employee starts the
bot using an invitation (`/start <token>`), or their approved numeric user ID.
Only `/help` and `/status` are handled as employee commands; native administrative
commands never reach the agent. Groups, bot messages, forwards and edits are ignored.

The sanitized inbox and run admission commit together; the polling offset advances
only after persistence. Responses use a durable outbox, with separate send intent
for each text part. Explicit rate limiting is retried after the provider delay.
An ambiguous send or connector restart during sending is marked uncertain without
blind retries. Access is checked again before each response. Provider acceptance
does not imply that the employee read the response. The connector has no Docker
socket, publishes no endpoint, and reads channel credential versions from the
existing secret volume. Slack reuses this admission and delivery path.

Install an internal Slack app from `deploy/slack-manifest.yaml`. Generate an
app-level token with `connections:write`, install the bot in the workspace, and
save its bot and app tokens together in the Slack channel. Bot scopes are
`im:history`, `chat:write` and `users:read`: the latter is used only for `bots.info`
to match the bot's app ID against the authenticated Socket Mode hello. The channel
also validates the configured workspace, granted scopes and a single active
Socket Mode connection before admitting messages. Different-app token pairs are
blocked. The app uses the Messages tab; employees initiate private conversations.
No public callback, OAuth wizard, group channel or Slack Connect access is added.

Socket Mode persists event admission before acknowledging each envelope. Retried
events with different envelope IDs share one persisted event and run. Bot echoes,
subtypes, shared conversations and workspace mismatches are ignored. A private
text message `register <invitation-token>` requests a pending identity; the admin
must approve it. `agent help` and `agent status` are private text commands too;
Slack slash commands are not registered by this app. Replies
use the original DM and the same shared durable outbox as Telegram. Slack SDK HTTP
automatic retries are disabled for sends; explicit rate limits are deferred and
ambiguous results stay uncertain. Token-bearing SDK protocol logs are suppressed.

Availability is read from expiring evidence. Agent readiness and the state of each
employee channel are reported independently; a Slack failure does not hide a
healthy Telegram access. Talos-managed routes also show allowance/assignment
blockers. Delivery history exposes provider acceptance and uncertain sends without
message bodies or secrets.

Admin actions request durable checks: `POST /api/v1/agents/{id}/checks` with
`{"kind":"runtime"}`, `{"kind":"connections"}` or `{"kind":"model"}`, plus an
`Idempotency-Key`. Checks share conversation admission and stay out of the default
admin history. Runtime checks read the owned runtime, and connection checks reuse
native setup/MCP discovery without calling business tools. Results from a changed
configuration are discarded. Completed conversations also provide recent evidence
that the model responded.

**A model test can consume provider credit.** It sends one bounded, tools-free
request through the effective Talos gateway route, using its normal accounting and
allowance admission. Native providers configured outside Talos have no portable
safe probe contract in this release: their explicit test returns
`native_safe_probe_unavailable`, rather than starting an ordinary agent chat.
Their actual completed conversations can still verify recent model availability.

In an agent's settings, **Access & availability** brings these admin controls
together: save write-only channel credentials, check a channel, register or invite
an employee, approve or revoke their identity, and copy platform instructions.
Employees receive Telegram/Slack links only. The screen distinguishes expiring
availability evidence from recorded delivery acceptance. Channel credentials are
excluded from the tool connection picker.

**Verify delivery** creates a single-use, 15-minute transport challenge for an
approved platform identity. Send `/verify <token>` in the private Telegram chat,
or `verify <token>` as ordinary private Slack text. This confirmation uses no
model or business tool. Its provider-accepted reply verifies transport only.
The employee must then send a normal text message: after the native agent
completes and every response part is accepted, Talos persists a delivery receipt.
Uncertain sends, incomplete replies and busy/error responses cannot verify delivery.

`POST /api/v1/employee-accesses/{id}/challenge` returns the token once with
`Cache-Control: no-store`; only its hash is stored. GET `/{id}/handoff` reads
metadata and historical receipts, never probes or sends. Tests are bound to the
identity/access revision, channel revision and credential version, agent
incarnation and Talos configuration fingerprint. Changed configurations require
a new test. Native edits made outside Talos are not automatically detected;
repeat delivery verification after such edits. Historical delivery acceptance
does not prove present availability or that a human read the message.

The optional Docker acceptance suite exercises both pinned native runtimes with
both adapters, isolated channel histories and native stop/start persistence:
`TALOS_NATIVE_CHANNEL_PROOF=1 python -m pytest -s
tests/integration/test_native_channel_acceptance.py`. Run inside the verification
image with a Docker socket and disposable PostgreSQL database. The suite uses
an internal Docker network, a local model provider and mocked Telegram/Slack
HTTP responses. It does not send external messages or spend provider credit.
For a live smoke, configure dedicated test bot/app credentials and test employee
IDs in the admin screen, explicitly approve them, then complete **Verify delivery**
from those accounts. Never use production employee recipients for an automated smoke.

`POST /api/v1/channels/{id}/check` queues a connector-owned check of bot/app identity,
scopes and transport, including Slack token pairing. Disabled Telegram channels
check only credentials and webhook compatibility; they do not poll or send messages.
Disabled Slack checks open a short-lived socket to validate the app token without
granting conversation access. Enabling a channel requires fresh transport evidence.
Read results with `GET /api/v1/channel-checks/{id}` or the channel's `/availability`.

Employee turns share the existing single active run admission with administrator
turns. Telegram, Slack and administrator conversations use independent native
sessions; the legacy administrator history is preserved. Revocation, reassignment
and credential rotation are checked again before runtime dispatch. Administrator
history defaults to admin turns; `GET /api/v1/agents/{id}/runs?source=employee`
shows employee turns separately (`source=all` includes every origin).

The local administrator API supports `/api/v1/employees` and `/api/v1/roles`
(GET/POST), their `/{id}` resources (PUT/DELETE), and GET `/api/v1/capabilities`.
Employees have one role; roles select native capability groups. Referenced records
cannot be deleted. These are administrator records, not employee login accounts.

Create an agent with `employee_id`, or attach a stopped agent with
`PUT /api/v1/agents/{id}/employee`. Legacy `employee_label` requests remain supported;
existing labels are never automatically converted into employee identities.
Saving a role leaves existing agent selections unchanged. Apply the saved role explicitly.

### Applying role permissions

For assigned native agents, the first Start captures the current role. Later starts
reuse the selected application, including setup and connection versions. Saving a
role or changing an employee's role leaves existing selections unchanged. `POST /api/v1/agents/{id}/apply-role` accepts an `Idempotency-Key`,
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

Hermes chat records a pending native turn before dispatch. After cancellation or
an unconfirmed process exit, the next turn uses Hermes' native rewind to archive
the unfinished request and tool calls, preserving earlier completed context and
the archived audit history. Files and other tool effects are not rolled back.
If compression rewrote the history (including within the same session), or a
later native user turn appeared, recovery refuses to resume automatically and
leaves history intact. Older pending records without a transcript checkpoint also
require review in Hermes before starting a new native chat. Completed turns keep
their native session identity.

### Durable provider accounting

New managed OpenRouter calls are admitted into a durable per-call ledger before
provider dispatch. Finalization updates that record once; failed persistence leaves
an unresolved record. Reported costs use fixed-precision USD values. Missing cost
is unknown, including interrupted calls without final provider accounting.
Employee attribution is captured at admission and remains unchanged after agent
reassignment or deletion. Legacy run usage remains readable but is not backfilled
into this ledger. Tracking begins when migration 0012 is applied.

Native OpenRouter requests through Talos use the same ledger, including native
workspace and channel calls without a Talos chat run. Streaming accounting observes
provider usage without changing tool calls or reasoning. Direct-provider traffic,
external tool charges, and infrastructure costs are outside this accounting.
Selecting OpenRouter does not prove that every request from a native runtime
passes through Talos; “Handled by agent” usage is unavailable to Talos.

### Usage reporting

Open **Usage** to inspect UTC calendar months by employee and agent, including
retained history for deleted agents. Known spending is the sum of reported charges;
missing-cost and unresolved calls are shown separately. Reassigning an agent never
moves existing employee charges. Months before tracking are unavailable, and the
first tracking month is explicitly partial. Legacy run JSON is not counted here.

`GET /api/v1/usage` returns totals, employee/agent breakdowns, coverage and filter
options. `GET /api/v1/usage/calls` returns paginated call details. Both accept
`month=YYYY-MM`, `employee_id` and `agent_id`; filters intersect. Call pages accept
`limit` (1–100, default 50) and the opaque `next_cursor` from the previous response.
New reporting APIs serialize USD values as decimal strings; unknown costs are null.

### Employee monthly allowances

Edit an employee and use **Monthly allowance (USD)** to set a shared allowance for
all their agents. Empty means unlimited (the default); zero blocks real-provider
requests through Talos. Alerts appear inside Talos at 80% and 100% of the allowance.
The Usage page, employee editor, agent settings and chat show current budget status.
Historical usage pages do not compare past spending against today's allowance.

Each managed or native OpenRouter call requires an employee assignment and checks
known spending immediately before provider dispatch. Allowances use UTC calendar
months; the entire call belongs to its admission month, including late completion.
Budget edits apply to subsequent admissions. Already-admitted calls can finish,
and concurrent calls can exceed the allowance. There are no reservations, rollover
credits, or mid-request cancellation when an allowance changes. Unknown costs warn
but do not block admission; this is not a guaranteed maximum provider bill.

`GET /api/v1/employees/{id}/budget` reports the current allowance, known spend,
UTC period, and status. `PUT` accepts `{ "monthly_allowance_usd": "25.00" }` or
JSON null for that field to remove the limit. Ordinary employee updates do not
change the allowance. Provider admission failures return OpenAI-compatible errors:
402 `employee_budget_exceeded`, 403 `employee_assignment_required`, or 503
`accounting_unavailable`. Denied requests do not contact OpenRouter. The offline
simulator remains available without an employee assignment.

**Upgrade order:** deploy ledger accounting, native accounting, and usage visibility
before this enforcement release. Before upgrading to the employee-budget release,
assign employees to existing agents that use Talos-routed inference (stop an agent,
assign it in Settings, then start it). Unassigned provider requests will be rejected
after the upgrade, even though every employee's initial allowance is unlimited.
Native providers configured outside Talos remain outside both accounting and
allowance enforcement. This feature does not add employee login accounts.


## Reusable agent setups

A setup is a versioned recipe for native OpenClaw and Hermes agents. Configure one reference agent, stop it, choose **Create setup from this agent**, review its captured skills and MCP connectors, then publish a version. Alternatively upload a prepared setup ZIP on **Setups**. Assign a published version to a role, select connector grants and account connections, and apply the role to selected agents.

Published setup versions are immutable. Publishing a new version does not change roles; select the version on a role explicitly. Saving roles or rotating credentials does not change an existing agent's selected configuration. **Start preserves the selected application**, including permissions and connection versions. Use **Apply saved role** to adopt changes. Applying to a running agent interrupts its work and restarts it; applying to a stopped agent leaves it stopped. Preflight failures preserve a running agent; failures after stopping leave it stopped until retried. Employee reassignment requires Apply before Start.

### Capturing an existing agent

Capture requires a stopped native agent with no unresolved work. The worker reads its private state with a read-only mount and networking disabled. It reads raw configuration without loading plugins or expanding credentials. The captured draft contains candidate skill folders, supported MCP definitions, and review information. Account values become named connection requirements. Personal credential files, sessions, memory, and unrelated workspace files are excluded; review selected skill source files as you would any code before publishing.

A manually installed tool is portable only when its complete runnable payload is available. Commands that depend on global installations, package downloads, outside paths, or native plugins appear as unresolved requirements. Supply a prepared bundle or remove the candidate before publication. Capture does not clone the agent's identity or automatically install anything on the source agent. Hermes may copy its bundled skills into the state directory during startup, so capture can list them alongside custom skills. Exclude skills already supplied by the pinned runtime when reviewing the draft; their native copies remain available and duplicate names would block application.

### Using a captured setup on another runtime

A setup captured from Hermes can also be applied to OpenClaw, and vice versa. In the draft's **Compatibility** section, choose **Add runtime target** and select the other runtime and Linux architecture. Keep both targets to share one setup version and role across Hermes and OpenClaw agents, or remove the original target to publish a setup for the destination runtime only. Publish the reviewed version, select it on the role, then explicitly apply it to the destination agents. Exported ZIPs retain these target declarations.

The same instructions, skill files, and connector payloads are shared across targets; Talos translates their native directories, MCP configuration, and tool permissions for each runtime. Review instructions and skill scripts that rely on runtime-specific commands or paths. This does not convert native plugins or make incompatible dependencies portable. For local connectors, expand **Interpreter requirements for local connectors** and pin each target's actual Node major or Python version. Changing a target's runtime clears its previous interpreter pins. Prepared payloads must work with every declared architecture and interpreter; use separate setups when they need different files.

Adding a target declares intended compatibility. Apply still checks the destination runtime, release, architecture, interpreter, skill-name conflicts, and available connector tools before reporting readiness. No compatibility check is bypassed, and the source agent is unchanged.

### Prepared bundle format

A ZIP contains `manifest.json` and its declared files. The manifest uses `schema_version: 1` and includes `instructions`, `targets`, `skills`, `connectors`, `connection_slots`, `assets` (relative path to SHA-256), and `unresolved` (empty for publication). The optional `executables` list identifies executable asset paths; when omitted on import, Talos derives it from ZIP permission bits. If supplied, it must match those bits. Export normalizes file permissions to 0755 for executable assets and 0644 for other files; application uses private 0700/0600 permissions and checks execution permission as well as content hashes. Capture preserves executable status without running the files. Each target declares `runtime_kind`, the exact Talos `runtime_release`, and `architecture` (`amd64` or `arm64`). Local Node/Python connectors also require the matching `node_major` or `python_version`.

Skill entries declare `id`, `name`, `path` under `skills/`, and `enabled`. Include the complete directory, starting with `SKILL.md`. IDs use lowercase letters, digits, and hyphens and begin with a letter. Avoid skill names already provided by the selected runtime or the destination agent; shadowing blocks application rather than silently selecting different instructions.

Connectors declare `id`, `name`, `enabled`, `transport`, and an explicit `tools` list. Hosted MCP supports `streamable-http` and `sse`, with a URL and optional headers. URLs must not contain credentials or query strings. Local MCP uses `stdio`, a `node` or `python3` runner, a relative `entrypoint` inside `connectors/<id>/`, optional arguments/environment, and dependency `provenance`. Include all vendored dependencies and their lock/provenance files. Talos does not fetch packages, run installation scripts, or resolve `npx`/`uvx` commands. Native dependencies must match the target Linux architecture, interpreter ABI, and runtime libraries.

Environment/header values are either non-secret strings or references such as `{"slot":"crm","field":"token"}`. Declare each slot in `connection_slots`, for example `{"id":"crm","label":"CRM account","fields":["token"]}`. Put actual credential values only in **Settings → Connections**. Roles select organization defaults; Employees can override individual slots. An invalid override blocks application instead of falling back to another account.

Archive import validates paths, links, duplicate entries, sizes, and content hashes without executing anything. Publication validates the complete manifest. Runtime application validates compatibility and native discovery. These are distinct checks: importing a ZIP does not prove its tools are usable. Native runtime permissions apply; terminal access still permits file and network operations beyond dedicated tool grants.

### Account changes, verification, and backups

Connection credentials are write-only. Rotation creates a new credential version and makes an update available; existing selections retain their previous version until Apply. Credential versions remain available while referenced by an agent or active operation. Detach bindings and apply the replacement before deleting a referenced connection. Agent deletion releases its snapshot references only after the worker confirms deletion; retained audit snapshots do not keep secrets alive. Provider-side revocation remains controlled by that provider.

The worker verifies skill discovery and the actual allowed MCP tool names without performing business actions. An installed setup is distinct from a currently verified ready agent. Hosted services can change outside Talos; reproducibility covers the declared artifacts, configuration, and permissions.

Include the `setup-artifacts` and `connection-secrets` volumes in the consistent backups described above, together with PostgreSQL and agent state. The API and worker share service group 10001: credential files use mode 0640 in a mode-0750 directory, and the worker mounts that volume read-only. Setup artifacts use mode 0640 in a mode-2770 directory so API uploads and worker captures remain mutually readable. Agents receive only their selected values in mode-0600 private environment files. Protect host access and backups as for existing native credentials. Restore code, database, artifacts, secrets, and runtime images together.

OAuth sign-in, package-registry discovery, native plugin installation, and department inheritance are outside this first version. One Talos installation remains one organization.
