# Administrator authentication checkpoint

## Scope

One built-in `admin`; host bootstrap/reset; browser login/logout; eight-hour revocable
PostgreSQL sessions; management API authentication; React gate; persistent cooldown;
explicit browser origins; focused tests and deployment/recovery documentation.
No multi-admin, RBAC, SSO/MFA, email recovery, audit UI, offboarding or unrelated changes.

## Route classification

- Protected: all routers included by `backend/app/main.py`, plus `/api/v1/status`.
  Covers agents/operations, diagnostics/runs/events/cancel, availability/checks,
  employees/roles/capabilities/budgets, channels/accesses/handoff, inference/provider,
  connections and setups (including multipart imports and binary asset/export reads).
- Public: `/health/live`, `/health/ready`, static UI, minimal auth session status,
  login and logout (logout is idempotent but still requires browser mutation protections).
- No dashboard WebSockets/SSE: run events are paged HTTP polling. Model streaming is
  in the separate gateway app and retains workload-token authentication.
- Worker/connector use the DB directly; Telegram long polling and Slack Socket Mode
  have no dashboard ingress endpoint. Preserve their independent authorization.
- Disable public OpenAPI/docs to keep the dashboard-facing surface minimal.

## Stack

1. `feat/admin-auth-storage` → `main`: migration, hashing, host commands and tests.
2. `feat/admin-auth-api` → storage: endpoints, sessions, shared management protection,
   browser protections, authenticated existing test fixtures and backend tests.
3. `feat/admin-auth-ui` → API: gate, setup/login/logout, shared fetch, cross-tab clearing.
4. `feat/admin-auth-verification` → UI: final integration, documentation, verification fixes.

Each level stays buildable. Storage alone does not secure the API. API level locks
the old dashboard until UI level is installed. All four remain draft; no deploy/merge.

## Acceptance criteria

- Bootstrap once (including concurrent calls), exact 15–128 character passwords with
  spaces/Unicode, hidden prompts; reset revokes every session atomically with login locking.
- Cryptography scrypt `N=2^17,r=8,p=1`, random salts; no secret output/body logging.
- Hash-only random tokens, HttpOnly/SameSite=Strict cookie, eight-hour fixed expiry,
  Secure default with explicit local HTTP setting; restart persistence and logout revocation.
- Five failed attempts trigger a 60-second cooldown, persisted/serialized on admin row.
- Every management read/mutation denied without a valid session, including before setup
  and on DB failure; exact allowed Origin plus required custom header for mutations.
- Auth gate mounts/fetches management only after authentication; logout, expiry, recovery
  and cross-tab events unmount/clear data and local management caches.
- Existing worker/gateway and Telegram/Slack tests continue to pass independently.

## Progress and evidence

- Inspected main at `9d8411b`, migrations through 0023, all API routes, all frontend fetches,
  Compose/Dockerfile, existing PostgreSQL test fixtures. No applicable AGENTS.md found.
- Isolated worktree: `/private/tmp/talos-admin-auth`; unrelated primary changes preserved.
- PR1 storage implemented: 173 unit/focused PostgreSQL tests passed, 1 existing skip;
  Ruff check passed. Concurrent bootstrap proved exactly one winner. Frontend build checked.
  Formatter-only changes to seven pre-existing files were discarded to preserve scope.
- Docker daemon is stopped; use disposable local PostgreSQL for backend verification.

## Remaining work

None. The full feature, documentation and draft stack are complete. See the delivery
audit below for evidence and the optional checks that could not run.

### API layer

- Storage: `836d27c`, draft https://github.com/JaviChulvi/talos/pull/47.
- API implemented; shared dependency protects every management route. Admission read
  ends before existing explicit business transactions. Three public auth endpoints only.
- 414 tests passed / 20 optional skips against disposable PostgreSQL; Ruff check, changed
  Python formatting, frontend lint/typecheck and Compose config passed. Native Docker
  acceptance unavailable because daemon is stopped; worker/gateway/adapter suite passed.
- Cookie Secure defaults true; `.env.example` explicitly selects local HTTP. Browser
  mutations require X-Talos-Request and exact configured Origin when present.
- Focused rerun checks fresh application instances for session/cooldown persistence.
- API level deliberately locks old UI until the next stack level.

### Dashboard layer

- API: `72c8453`, draft https://github.com/JaviChulvi/talos/pull/48.
- Gate implemented with existing UI controls and one shared raw/JSON fetch path. Only
  auth status is fetched before setup/login; all dashboard components mount afterward.
- React/native storage events clear other tabs; operation ID cache removed on logout.
- Frontend lint/typecheck/build passed. Real browser setup, wrong password, pre-auth
  request isolation, login, reload persistence, cookie flags and two-tab logout verified.
- Actual host bootstrap prompts hid the synthetic Unicode/spaced password correctly.
- Local browser proof uses disposable DB on port 55439 and API preview port 18004.
- Remaining: publish UI draft; final process restart/cooldown/reset/expiry browser proof,
  complete docs, final regression checks, adjacent/full stack links, cleanup.

### Final verification layer

- UI: `74d20f5`, draft https://github.com/JaviChulvi/talos/pull/49.
- Actual API subprocess restart proved both session and cooldown persistence. A real
  PostgreSQL trigger failure proved reset rolls back both password and session deletion.
- Full suite: 417 passed, 20 optional skips (68.83s). Native container acceptance is
  unverified because Docker is stopped; independent worker/gateway/Slack/Telegram suite passed.
- Real browser verified reset clears both tabs and 128-character Unicode login works.
  Manually advanced stored expiry cleared private views. Actual disposable-DB outage
  returned 503 and cleared management UI, then DB was restored. Hidden host recovery
  prompts verified. This is local acceptance only; no real employee messages/provider charges.
- Final fixes: refuse getpass echo fallback; bound session status fetch to 8 seconds;
  clear outage error after revalidation; wrap host commands for smaller screens.
- README now reflects authentication, first boot, upgrades, local HTTP, HTTPS proxy,
  origin/Host configuration, recovery and focused process verification.
- All final build/browser checks passed. All four drafts open; live GitHub audit confirmed
  correct preceding-branch bases and only incremental commits/files. All descriptions
  linked; proof session revoked and disposable API/PostgreSQL processes stopped.

- Final UI lint/typecheck/build, Ruff check and changed-Python formatting passed.
  Global Ruff format check still flags seven files identical to origin/main; none were
  modified by this feature. Existing Vite >500KB bundle warning remains.
- Final browser rerun passed after rebuilding: no pre-auth management requests, Unicode
  login, and outage recovery clears its error. Two-tab logout took 142ms in local proof.

## Delivery and completion audit

| Level | Branch | Functional commit | Draft PR | Base |
| --- | --- | --- | --- | --- |
| 1 | feat/admin-auth-storage | 836d27c | https://github.com/JaviChulvi/talos/pull/47 | main |
| 2 | feat/admin-auth-api | 72c8453 | https://github.com/JaviChulvi/talos/pull/48 | feat/admin-auth-storage |
| 3 | feat/admin-auth-ui | 74d20f5 | https://github.com/JaviChulvi/talos/pull/49 | feat/admin-auth-api |
| 4 | feat/admin-auth-verification | 9f5009f | https://github.com/JaviChulvi/talos/pull/50 | feat/admin-auth-ui |

The final checkpoint-only commit follows the functional head above. All PRs are attached
to this chat. First PR describes the full stack; adjacent drafts are linked in descriptions.
No PR was merged and no production deployment performed.

Requirement evidence:
- Singleton/concurrent bootstrap: real PostgreSQL tests in test_admin_auth.py; id=1 DB check.
- Exact password limits/Unicode/spaces, salts and safe prompts: test_admin_password.py,
  real interactive CLI bootstrap/reset and browser 128-codepoint Unicode login.
- Atomic reset/revocation/race/rollback: tests in test_admin_auth.py, including DB trigger failure.
- Cookies/fixed expiry/no renewal/logout/invalid sessions: test_admin_auth.py; actual browser reload
  and forced stored-expiry proof. Eight-hour duration verified from timestamps, not an eight-hour wait.
- API process and cooldown persistence: test_admin_auth_process.py restarts actual Uvicorn processes.
- Concurrent cooldown: eight concurrent wrong logins yield four 401s/four 429s; stored counter=5.
- Every management read/mutation: route enumeration test covers all dashboard API operations.
  No management SSE/WebSocket; exports/assets are finite byte Responses, also authenticated.
- DB denial: injected DB failure test plus actual disposable-DB outage returned 503; UI cleared.
- Browser mutation protections: tests cover login/logout/agent requests with rejected origins
  and missing header; actual configured-origin browser login succeeded. Forwarded headers unused.
- Gate/cache/cross-tab/expiry/reset: browser automation against compiled production UI, synthetic
  private role record and operation cache; only auth status fetched before login.
- Service independence: full existing worker/gateway/Telegram/Slack suite passed; native Docker
  acceptance remains an explicitly unverified optional check while Docker daemon is stopped.
- Docs: README setup/login/reset, local HTTP and HTTPS proxy config, upgrade and verification.

Final results: 417 passed / 20 optional skipped; frontend lint/typecheck/build, Ruff check,
changed-file formatting, Compose config and git diff whitespace checks passed. Seven global
formatting failures are byte-identical to main. Existing Vite bundle-size warning retained.

- Cleanup verified: API process exited cleanly, disposable PostgreSQL stopped, proof
  browser tab closed. Worktree retained for review/follow-up. Primary checkout preserved.

## Review fixes (2026-10-01)

- PR #48: `c75b7d8` filters the Talos administrator cookie from native HTTP/WebSocket
  requests and responses, preserving native cookies, uploads, frames and rejection status.
  Worker recovery upgrades legacy relays on their existing ports without restarting agents;
  failed creation/start retries retain the port, and Stop never starts a relay. HTTPS host
  documentation retains `127.0.0.1` for Compose readiness. Nine focused relay/health checks
  passed, plus 78 administrator/lifecycle/native checks with one optional skip.
- PR #49: `7c40d96` bounds login and logout to eight seconds. Actual browser requests held
  pending returned error/retry controls after 8.4 seconds for both actions.
- PR #50: additive merges carry both owning-PR fixes into the final stack without rewriting
  existing commits. Final suite: 424 passed / 20 optional skips in 67.91 seconds. Frontend
  lint/typecheck/build, Ruff, changed-file formatting, Node syntax, Compose and whitespace
  checks passed. The existing bundle warning remains; native Docker acceptance is unavailable
  because its daemon is stopped.
- The original cookie leak was reproduced in a browser and then checked through the actual
  Node relay with a local native-service fixture: admin cookie absent upstream, native cookie
  present/renewed, malicious native Set-Cookie unable to overwrite the admin cookie.
- Independent security investigation and candidate review completed; upgrade retry and Stop
  regressions found in the first candidate were corrected and covered before publication.
