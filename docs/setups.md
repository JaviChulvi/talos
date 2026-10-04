# Reusable agent setups

[← Talos](../README.md) · [Documentation index](../README.md#documentation)

Capture, review, and publish versioned instructions, skills, and MCP connectors, then apply them through user profiles.

A setup is a versioned recipe for native OpenClaw and Hermes agents. Configure one reference agent, stop it, choose **Create setup from this agent**, review its captured skills and MCP connectors, then publish a version. Alternatively upload a prepared setup ZIP on **Setups**. Assign a published version to a profile, select connector grants and account connections, and apply the profile to selected agents.

Published setup versions are immutable. Publishing a new version does not change profiles; select the version on a profile explicitly. Saving profiles or rotating credentials does not change an existing agent's selected configuration. **Start preserves the selected application**, including permissions and connection versions. Use **Apply saved profile** to adopt changes. Applying to a running agent interrupts its work and restarts it; applying to a stopped agent leaves it stopped. Preflight failures preserve a running agent; failures after stopping leave it stopped until retried. User reassignment requires Apply before Start.

## Capturing an existing agent

Capture requires a stopped native agent with no unresolved work. The worker reads its private state with a read-only mount and networking disabled. It reads raw configuration without loading plugins or expanding credentials. The captured draft contains candidate skill folders, supported MCP definitions, and review information. Account values become named connection requirements. Personal credential files, sessions, memory, and unrelated workspace files are excluded; review selected skill source files as you would any code before publishing.

A manually installed tool is portable only when its complete runnable payload is available. Commands that depend on global installations, package downloads, outside paths, or native plugins appear as unresolved requirements. Supply a prepared bundle or remove the candidate before publication. Capture does not clone the agent's identity or automatically install anything on the source agent. Hermes may copy its bundled skills into the state directory during startup, so capture can list them alongside custom skills. Exclude skills already supplied by the pinned runtime when reviewing the draft; their native copies remain available and duplicate names would block application.

## Using a captured setup on another runtime

A setup captured from Hermes can also be applied to OpenClaw, and vice versa. In the draft's **Compatibility** section, choose **Add runtime target** and select the other runtime and Linux architecture. Keep both targets to share one setup version and profile across Hermes and OpenClaw agents, or remove the original target to publish a setup for the destination runtime only. Publish the reviewed version, select it on the profile, then explicitly apply it to the destination agents. Exported ZIPs retain these target declarations.

The same instructions, skill files, and connector payloads are shared across targets; Talos translates their native directories, MCP configuration, and tool permissions for each runtime. Review instructions and skill scripts that rely on runtime-specific commands or paths. This does not convert native plugins or make incompatible dependencies portable. For local connectors, expand **Interpreter requirements for local connectors** and pin each target's actual Node major or Python version. Changing a target's runtime clears its previous interpreter pins. Prepared payloads must work with every declared architecture and interpreter; use separate setups when they need different files.

Adding a target declares intended compatibility. Apply still checks the destination runtime, release, architecture, interpreter, skill-name conflicts, and available connector tools before reporting readiness. No compatibility check is bypassed, and the source agent is unchanged.

## Prepared bundle format

A ZIP contains `manifest.json` and its declared files. The manifest uses `schema_version: 1` and includes `instructions`, `targets`, `skills`, `connectors`, `connection_slots`, `assets` (relative path to SHA-256), and `unresolved` (empty for publication). The optional `executables` list identifies executable asset paths; when omitted on import, Talos derives it from ZIP permission bits. If supplied, it must match those bits. Export normalizes file permissions to 0755 for executable assets and 0644 for other files; application uses private 0700/0600 permissions and checks execution permission as well as content hashes. Capture preserves executable status without running the files. Each target declares `runtime_kind`, the exact Talos `runtime_release`, and `architecture` (`amd64` or `arm64`). Local Node/Python connectors also require the matching `node_major` or `python_version`.

Skill entries declare `id`, `name`, `path` under `skills/`, and `enabled`. Include the complete directory, starting with `SKILL.md`. IDs use lowercase letters, digits, and hyphens and begin with a letter. Avoid skill names already provided by the selected runtime or the destination agent; shadowing blocks application rather than silently selecting different instructions.

Connectors declare `id`, `name`, `enabled`, `transport`, and an explicit `tools` list. Hosted MCP supports `streamable-http` and `sse`, with a URL and optional headers. URLs must not contain credentials or query strings. Local MCP uses `stdio`, a `node` or `python3` runner, a relative `entrypoint` inside `connectors/<id>/`, optional arguments/environment, and dependency `provenance`. Include all vendored dependencies and their lock/provenance files. Talos does not fetch packages, run installation scripts, or resolve `npx`/`uvx` commands. Native dependencies must match the target Linux architecture, interpreter ABI, and runtime libraries.

Environment/header values are either non-secret strings or references such as `{"slot":"crm","field":"token"}`. Declare each slot in `connection_slots`, for example `{"id":"crm","label":"CRM account","fields":["token"]}`. Put actual credential values only in **Settings → Connections**. Agent profiles select shared defaults; Users can override individual slots. An invalid override blocks application instead of falling back to another account.

Archive import validates paths, links, duplicate entries, sizes, and content hashes without executing anything. Publication validates the complete manifest. Runtime application validates compatibility and native discovery. These are distinct checks: importing a ZIP does not prove its tools are usable. Native runtime permissions apply; terminal access still permits file and network operations beyond dedicated tool grants.

## Account changes, verification, and backups

Connection credentials are write-only. Rotation creates a new credential version and makes an update available; existing selections retain their previous version until Apply. Credential versions remain available while referenced by an agent or active operation. Detach bindings and apply the replacement before deleting a referenced connection. Agent deletion releases its snapshot references only after the worker confirms deletion; retained audit snapshots do not keep secrets alive. Provider-side revocation remains controlled by that provider.

The worker verifies skill discovery and the actual allowed MCP tool names without performing business actions. An installed setup is distinct from a currently verified ready agent. Hosted services can change outside Talos; reproducibility covers the declared artifacts, configuration, and permissions.

Include the `setup-artifacts` and `connection-secrets` volumes in the consistent [source-checkout backups](operations.md#source-checkout-maintenance) or [release-bundle backups](installation.md#status-and-manual-maintenance), together with PostgreSQL and agent state. The API and worker share service group 10001: credential files use mode 0640 in a mode-0750 directory, and the worker mounts that volume read-only. Setup artifacts use mode 0640 in a mode-2770 directory so API uploads and worker captures remain mutually readable. Agents receive only their selected values in mode-0600 private environment files. Protect host access and backups as for existing native credentials. Restore code, database, artifacts, secrets, and runtime images together.

OAuth sign-in, package-registry discovery, native plugin installation, and configuration inheritance are outside this first version. Each Talos installation has a single administrator and shared configuration.
