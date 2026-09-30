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

Complete all four levels, run checks at each level, open/link/attach draft PRs, verify browser
behavior, finalize evidence and limitations. Keep this checkpoint updated per level.
