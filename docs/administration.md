# Administration and user access

[← Talos](../README.md) · [Documentation index](../README.md#documentation)

The dashboard and management API are for the installation administrator. Users chat through their approved private Telegram or Slack identity. Start with [your first agent](../README.md#your-first-agent), then use this guide for authentication, channels, delivery verification, and profile permissions. Source-checkout commands below use Docker Compose; a release installation creates the administrator through its [installer](installation.md).

## Users, agent profiles, and setups

A **user** is the person assigned to an agent, with optional personal connections
and a monthly budget. These records do not create dashboard login accounts.
An **agent profile** selects capabilities, a published setup version, tool grants,
and default connections. A **setup** packages reusable instructions, skills, and
tools. Profiles such as Research, Coding, or General purpose can be reused across users.

### Upgrading to the generic terminology

Migration `0027` renames the existing records in place. IDs, assignments, budgets,
spending history, approved channel identities, and queued profile applications are
preserved. User-authored names, instructions, and conversation content are unchanged.
Native conversation identifiers and receipt hashes retain their stable wire format
so existing chat history and installed setups remain usable.

The management API uses the new names without legacy route or field aliases:

| Previous API name | Current API name |
| --- | --- |
| `/employees`, `/roles` | `/users`, `/profiles` |
| `/employee-accesses` | `/user-accesses` |
| `/agents/{id}/employee`, `/agents/{id}/apply-role` | `/agents/{id}/user`, `/agents/{id}/apply-profile` |
| `employee_id`, `employee_label`, `employee_name` | `user_id`, `user_label`, `user_name` |
| `role_id`, `role`, `applied_role`, `role_application` | `profile_id`, `profile`, `applied_profile`, `profile_application` |
| `monthly_allowance_usd`, usage `allowances` | `monthly_budget_usd`, usage `budgets` |
| `source=employee`, operation `apply_role` | `source=user`, operation `apply_profile` |

Paths above are relative to `/api/v1`. Usage filters and breakdowns use `user_id`
and `users`; provider admission errors use `user_assignment_required` and
`user_budget_exceeded`. Update API clients and bookmarks (`#users`, `#profiles`)
alongside the application.

Run the migration with API, worker, gateway, and connector services stopped, then
start every service from the same new revision. Use the normal
[installation update and backup procedure](installation.md#status-and-manual-maintenance)
for packaged releases. For a source checkout, stop those services, rebuild the
platform images, run `docker compose run --rm migrate`, and restart the services.
Migration downgrade restores the previous schema and snapshot keys; stop the
services before downgrading and run the matching older application code afterward.

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
`TALOS_ADMIN_COOKIE_SECURE=true`, `TALOS_ALLOWED_HOSTS=["talos.example.com","127.0.0.1"]`, and
`TALOS_ALLOWED_ORIGINS=["https://talos.example.com"]`. Pass the public Host and browser
Origin unchanged. The API uses those configured values, never `X-Forwarded-*`, for
browser admission/cookie settings. Configure the proxy's request logging to exclude
Cookie/Set-Cookie and bodies. Native runtime UI relays retain their separate loopback
access and runtime authentication. Their HTTP/WebSocket relay removes the Talos admin
cookie in both directions while preserving native cookies. Worker recovery replaces
older TCP relays on the same port without restarting the native agent. Keep the loopback
host in the allowlist for Compose's readiness check.

## User and profile administration

User channel configuration is admin-only. `/api/v1/channels` configures one
Telegram bot and one Slack workspace app; channels are disabled initially. Store
write-only credentials with `PUT /api/v1/channels/{id}/credentials` using
`{"values":{"bot_token":"..."}}` for Telegram, or both `app_token` and `bot_token`
for Slack. These immutable credential versions cannot be assigned to profiles or
agents, and rotation invalidates channel verification.

`/api/v1/user-accesses` associates a user's assigned native agent with a
stable platform user ID (and Slack workspace ID). New accesses are pending.
`POST /{id}/invitation` returns a single-use token valid for 15 minutes; claiming
it remains pending until the admin calls `POST /{id}/approve`. `POST /{id}/disable`
revokes access. No user login or public Talos API is introduced. Messaging
transport and guided handoff use these records without user Talos accounts.

The `connector` service receives private Telegram text messages by long polling.
Enable the configured channel after saving its bot token. It verifies `getMe`
and refuses a conflicting webhook or polling consumer. The user starts the
bot using an invitation (`/start <token>`), or their approved numeric user ID.
Only `/help` and `/status` are handled as user commands; native administrative
commands never reach the agent. Groups, bot messages, forwards and edits are ignored.

The sanitized inbox and run admission commit together; the polling offset advances
only after persistence. Responses use a durable outbox, with separate send intent
for each text part. Explicit rate limiting is retried after the provider delay.
An ambiguous send or connector restart during sending is marked uncertain without
blind retries. Access is checked again before each response. Provider acceptance
does not imply that the user read the response. The connector has no Docker
socket, publishes no endpoint, and reads channel credential versions from the
existing secret volume. Slack reuses this admission and delivery path.

Channel replies are limited to 24,000 characters, split into provider-sized parts.
Longer replies include an explicit truncation notice within that limit. Their delivery
code is `response_truncated`; accepting the shortened reply does not verify complete
response delivery. The full answer remains in the saved run.

Correct a Slack workspace ID with **Save workspace** in Access & availability,
or `PUT /api/v1/channels/{id}` with `name`, `enabled`, and `workspace_id`.
A changed workspace always disables the channel, clears verification, and makes
user accesses pending with outstanding invitations invalidated. Check and
enable the channel, then save and approve identities in the corrected workspace.
Existing credentials are retained; rotate them if they belong to another workspace.

Install an internal Slack app from `deploy/slack-manifest.yaml`. Generate an
app-level token with `connections:write`, install the bot in the workspace, and
save its bot and app tokens together in the Slack channel. Bot scopes are
`im:history`, `chat:write` and `users:read`: the latter is used only for `bots.info`
to match the bot's app ID against the authenticated Socket Mode hello. The channel
also validates the configured workspace, granted scopes and a single active
Socket Mode connection before admitting messages. Different-app token pairs are
blocked. The app uses the Messages tab; users initiate private conversations.
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
user channel are reported independently; a Slack failure does not hide a
healthy Telegram access. Talos-managed routes also show budget/assignment
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
budget admission. Native providers configured outside Talos have no portable
safe probe contract in this release: their explicit test returns
`native_safe_probe_unavailable`, rather than starting an ordinary agent chat.
Their actual completed conversations can still verify recent model availability.

In an agent's settings, **Access & availability** brings these admin controls
together: save write-only channel credentials, check a channel, register or invite
a user, approve or revoke their identity, and copy platform instructions.
Users receive Telegram/Slack links only. The screen distinguishes expiring
availability evidence from recorded delivery acceptance. Channel credentials are
excluded from the tool connection picker.

**Verify delivery** creates a single-use, 15-minute transport challenge for an
approved platform identity. Send `/verify <token>` in the private Telegram chat,
or `verify <token>` as ordinary private Slack text. This confirmation uses no
model or business tool. Its provider-accepted reply verifies transport only.
The user must then send a normal text message: after the native agent
completes and every response part is accepted, Talos persists a delivery receipt.
Uncertain sends, incomplete replies and busy/error responses cannot verify delivery.

`POST /api/v1/user-accesses/{id}/challenge` returns the token once with
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
For a live smoke, configure dedicated test bot/app credentials and test user
IDs in the admin screen, explicitly approve them, then complete **Verify delivery**
from those accounts. Never use production user recipients for an automated smoke.

`POST /api/v1/channels/{id}/check` queues a connector-owned check of bot/app identity,
scopes and transport, including Slack token pairing. Disabled Telegram channels
check only credentials and webhook compatibility; they do not poll or send messages.
Disabled Slack checks open a short-lived socket to validate the app token without
granting conversation access. Enabling a channel requires fresh transport evidence.
Read results with `GET /api/v1/channel-checks/{id}` or the channel's `/availability`.

User turns share the existing single active run admission with administrator
turns. Telegram, Slack and administrator conversations use independent native
sessions; the legacy administrator history is preserved. Revocation, reassignment
and credential rotation are checked again before runtime dispatch. Administrator
history defaults to admin turns; `GET /api/v1/agents/{id}/runs?source=user`
shows user turns separately (`source=all` includes every origin).

The local administrator API supports `/api/v1/users` and `/api/v1/profiles`
(GET/POST), their `/{id}` resources (PUT/DELETE), and GET `/api/v1/capabilities`.
Users have one profile; profiles select native capability groups. Referenced records
cannot be deleted. These are administrator records, not user login accounts.

Create an agent with `user_id`, or attach a stopped agent with
`PUT /api/v1/agents/{id}/user`. Legacy `user_label` requests remain supported;
existing labels are never automatically converted into user identities.
Saving a profile leaves existing agent selections unchanged. Apply the saved profile explicitly.

### Applying profile permissions

For assigned native agents, the first Start captures the current profile. Later starts
reuse the selected application, including setup and connection versions. Saving a
profile or changing a user's profile leaves existing selections unchanged. `POST /api/v1/agents/{id}/apply-profile` accepts an `Idempotency-Key`,
returns HTTP 202, and uses the existing operation polling endpoint. It interrupts
running work, applies the captured revision while stopped, and restarts only when
previously running. A newer edit remains pending. Failed applications leave the
agent stopped; inspect the operation error and native configuration before retrying.

Agent responses expose `profile` (saved), `applied_profile` (last successful snapshot),
and `permissions_pending`. Managed conversation agents keep their no-tools
contract; unassigned native agents keep their existing native configuration.

Agent profiles govern native tool availability and dispatch, not arbitrary-code containment.
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
