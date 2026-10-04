# Models, usage, and user budgets

[← Talos](../README.md) · [Documentation index](../README.md#documentation)

Use OpenRouter through Talos for shared credentials and per-user usage reporting, or configure a provider directly inside a native runtime. Talos accounting and budgets cover only requests routed through its gateway. Run shell commands from the repository root.

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
Agent profile permissions, other provider credentials, workspace and history are preserved.

The shared OpenRouter key is read by the gateway, never copied into agents. Native agents receive an incarnation
token for `/native/v1/chat/completions`; this route preserves tool calls and results,
and accepts only active native agents with an OpenRouter selection. Native runtimes
still own tool dispatch and generation settings. Native inference usage is recorded in the durable call ledger, including calls
without a Talos chat run. User monthly budgets apply at gateway admission. The managed text-only route retains its no-tools contract.

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

The picker is part of the **administrator-authenticated** dashboard. Users access
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

## Durable provider accounting

New managed OpenRouter calls are admitted into a durable per-call ledger before
provider dispatch. Finalization updates that record once; failed persistence leaves
an unresolved record. Reported costs use fixed-precision USD values. Missing cost
is unknown, including interrupted calls without final provider accounting.
User attribution is captured at admission and remains unchanged after agent
reassignment or deletion. Legacy run usage remains readable but is not backfilled
into this ledger. Tracking begins when migration 0012 is applied.

Native OpenRouter requests through Talos use the same ledger, including native
workspace and channel calls without a Talos chat run. Streaming accounting observes
provider usage without changing tool calls or reasoning. Direct-provider traffic,
external tool charges, and infrastructure costs are outside this accounting.
Selecting OpenRouter does not prove that every request from a native runtime
passes through Talos; “Handled by agent” usage is unavailable to Talos.

## Usage reporting

Open **Usage** to inspect UTC calendar months by user and agent, including
retained history for deleted agents. Known spending is the sum of reported charges;
missing-cost and unresolved calls are shown separately. Reassigning an agent never
moves existing user charges. Months before tracking are unavailable, and the
first tracking month is explicitly partial. Legacy run JSON is not counted here.

`GET /api/v1/usage` returns totals, user/agent breakdowns, coverage and filter
options. `GET /api/v1/usage/calls` returns paginated call details. Both accept
`month=YYYY-MM`, `user_id` and `agent_id`; filters intersect. Call pages accept
`limit` (1–100, default 50) and the opaque `next_cursor` from the previous response.
New reporting APIs serialize USD values as decimal strings; unknown costs are null.

## User monthly budgets

Edit a user and use **Monthly budget (USD)** to set a shared budget for
all their agents. Empty means unlimited (the default); zero blocks real-provider
requests through Talos. Alerts appear inside Talos at 80% and 100% of the budget.
The Usage page, user editor, agent settings and chat show current budget status.
Historical usage pages do not compare past spending against today's budget.

Each managed or native OpenRouter call requires a user assignment and checks
known spending immediately before provider dispatch. Budgets use UTC calendar
months; the entire call belongs to its admission month, including late completion.
Budget edits apply to subsequent admissions. Already-admitted calls can finish,
and concurrent calls can exceed the budget. There are no reservations, rollover
credits, or mid-request cancellation when a budget changes. Unknown costs warn
but do not block admission; this is not a guaranteed maximum provider bill.

`GET /api/v1/users/{id}/budget` reports the current budget, known spend,
UTC period, and status. `PUT` accepts `{ "monthly_budget_usd": "25.00" }` or
JSON null for that field to remove the limit. Ordinary user updates do not
change the budget. Provider admission failures return OpenAI-compatible errors:
402 `user_budget_exceeded`, 403 `user_assignment_required`, or 503
`accounting_unavailable`. Denied requests do not contact OpenRouter. The offline
simulator remains available without a user assignment.

**Upgrading an older source installation:** before enabling budget enforcement,
assign users to existing agents that use Talos-routed inference (stop an agent,
assign it in Settings, then start it). Unassigned provider requests will be rejected
after the upgrade, even though every user's initial budget is unlimited.
Native providers configured outside Talos remain outside both accounting and
budget enforcement. This feature does not add user login accounts.
