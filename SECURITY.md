# Security policy

## Reporting a vulnerability

For a public Talos repository with private vulnerability reporting enabled, use
[Report a vulnerability](https://github.com/JaviChulvi/talos/security/advisories/new).
Reports submitted there are private to the maintainers and invited collaborators.
Do not put vulnerabilities, credentials, or private user data in public issues
or pull requests. A useful report includes the affected commit/version, deployment
and runtime, expected boundary, reproducible steps with synthetic data, and impact.

**Launch prerequisite:** while this repository is private, the public reporting
route is not available to outside researchers. Before public launch the owner must
enable GitHub Private Vulnerability Reporting, verify the reporting form using a
separate account, and verify that the maintainer receives report notifications.
This document does not assert that those external settings have been enabled.
No response-time SLA or bug bounty is currently offered.

## Supported versions

Talos is a development preview. Security fixes target current main and any explicitly
announced supported preview release. There is no maintained legacy release line.
The approved runtime catalog currently pins OpenClaw 2026.9.6 and Hermes 0.21.5;
upstream releases do not become supported automatically. Report an affected pinned
runtime to Talos as well as following the upstream project's disclosure policy.

## Trust boundaries

An installation uses a single shared workspace with a single administrator and trusted host
operators. The worker controls Docker; user agent containers do not receive the
Docker socket. Containers share a kernel and do not establish hostile-tenant isolation.
The dashboard and native runtime interfaces are for administration and testing.
Users use approved private Telegram or Slack identities.

Native tools and runtime-owned credentials remain powerful. Tool permissions are not
arbitrary-code containment, and prompt injection cannot be universally prevented.
Only Talos-routed model traffic contributes to its spending ledger; budgets are
not a guaranteed maximum bill. Cancellation cannot undo completed external actions.
Unknown deliveries require reconciliation and are not blindly replayed.

See [runtime boundaries](docs/runtimes.md), [access and authentication](docs/administration.md),
and [installation/recovery](docs/installation.md). Please report violations of the
stated boundaries, including authorization bypasses, cross-agent data access,
credential exposure, or unsafe replay/restore behavior.
