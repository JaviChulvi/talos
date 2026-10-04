# Preview installation and recovery

[← Talos](../README.md) · [Documentation index](../README.md#documentation)

This guide covers promoted preview bundles. For a source checkout, use [Development and validation](development.md#run-locally) and [source-checkout maintenance](operations.md#source-checkout-maintenance).

The release installer supports Ubuntu 24.04 x86_64 with local rootful Docker Engine,
and Apple Silicon macOS with local Docker Desktop. Allocate at least 4 CPUs,
8 GB RAM, and 30 GiB free disk space. Docker must already be installed and running.
Remote Docker contexts, rootless Docker, importing source-checkout installations,
and restoring across CPU architectures are outside this preview.

Release candidates are not installable releases until both native-host acceptance
jobs pass and the promotion workflow publishes the exact tested bundle. A successful
unit test run alone does not establish installation or recovery support.

## Install a published release

For public releases, download the assets from the official GitHub Releases page;
public GHCR images allow anonymous pulls. The `--bundle` path needs no GitHub CLI
or registry login. While previews remain private, authenticate GitHub CLI for
release downloads (`gh auth login`) and Docker for GHCR (`docker login ghcr.io`).
Use the ordinary host credential store; the installer does not copy registry
credentials into Talos. Private package tokens need `read:packages` and access.

Download every asset from one published release into a new directory:

```sh
mkdir talos-release
gh release download RELEASE_VERSION --repo JaviChulvi/talos --dir talos-release
bash talos-release/talos install --bundle "$PWD/talos-release"
```

Replace `RELEASE_VERSION` with a published version; `latest` is not an installation
version. Alternatively, download the complete assets from the official GitHub Releases page.
Keep `talos`, `manifest.json`, `release.env`, `compose.release.yaml`, image lists,
`checksums.txt`, root license/notice files, and `license*.tar.gz` together. Customers do not need Python, Node, Git, or source code.
The launcher checks the downloaded checksums before running the pinned management
container. Checksums detect corruption; obtain the bundle through the official HTTPS
release page. A checksum downloaded beside an artifact is not an independent signature.

The default data directory is `~/.talos`. Use `--directory /absolute/path` to change
it. The installer checks Docker and available resources, pulls exact images, creates
private secrets, runs database migrations, starts services, and prompts for the
administrator password in a terminal. Follow the displayed URL and setup checklist
to connect OpenRouter, assign a role and employee, choose a supported runtime, and
verify the employee's Telegram or Slack conversation. Administrator testing views
are separate from the employee's channel.

Rerun the same command after an interrupted download or installation. Retain the
installation directory: it holds the identity, secrets, selected release, and
operation journal needed to resume. Do not replace `.env` with a fresh template.
The `--skip-admin` option leaves administrator creation pending and is intended for
disposable acceptance fixtures, not a completed customer installation.

## Local access and Linux HTTPS

Local mode binds HTTP to loopback, using port 8000 by default. Use `--port 8080` for
a different local port. Native testing interfaces remain on loopback. PostgreSQL,
the internal model gateway, Docker, and Caddy administration are never published.

For a Linux server, point the desired domain's DNS to that server and allow inbound
TCP 80/443 before installation:

```sh
bash talos-release/talos install --bundle "$PWD/talos-release" --domain agents.example.com
```

Caddy provisions and renews certificates. Talos uses exact allowed origins/hosts and
secure administrator cookies. A DNS or certificate error is a resumable installation
failure. Keep Caddy's persistent data with the installation backups. Acceptance-only
ACME overrides are not a production fallback.

Linux services recover after the Docker daemon starts on reboot. On macOS, recovery
starts after Docker Desktop starts following sign-in. This is not an unattended
pre-login Mac service. Workers recheck durable intent, runtime ownership, permissions,
credentials, and pinned images. Intentionally stopped employees stay stopped;
unresolved work remains fenced for explicit reconciliation.

## Status and manual maintenance

Use the launcher from the downloaded bundle for each command:

```sh
bash talos-release/talos status
bash talos-release/talos doctor
bash talos-release/talos backup --archive /absolute/backups/talos.tar --identity /absolute/recovery/talos.age
```

Stop every employee agent first and resolve uncertain operations. Backup and update
will refuse a running native runtime; conversation counters alone do not demonstrate
that an employee's tools are idle. Maintenance fences new work and pauses remaining
writers before capturing state. There are no scheduled backups or unattended updates.

If a gateway or connector crash left unfinished inference or uncertain delivery,
inspect the provider's outcome and stop every employee before explicitly acknowledging
that uncertainty:

```sh
bash talos-release/talos status --reconcile-uncertain
```

This command fences admissions and confirms platform writers have stopped before
closing interrupted inference records and blocking uncertain sends. Unknown costs
remain unknown; existing costs, response parts and provider IDs are retained. It never
resends a message or marks an uncertain delivery successful. Resolve queued work and
channel checks first. If interrupted, rerun this same command; ordinary backup/update
commands still refuse unreconciled uncertainty.
If a backup or update already failed at its pre-write maintenance gate, use
`status --cancel-maintenance` to cancel that attempt before reconciliation.

The backup includes the database, installation secrets/configuration, setup artifacts,
worker credentials, owned runtime volumes, HTTPS state, and required local-only images.
The envelope's components are encrypted with `age` and checked before publication.
The recovery identity is created with private permissions if it does not already
exist. Keep it separately, outside the installation and archive, and copy it to your
recovery location. Losing it means losing access to the backup.

To restore to an empty directory on a second server of the same architecture, stop
and fence the original server first. Obtain the matching release bundle, authenticate
the target Docker host if its images are private, and copy the archive and recovery
identity securely:

```sh
bash talos-release/talos restore --directory /absolute/new-talos --bundle "$PWD/talos-release" --archive /absolute/backups/talos.tar --identity /absolute/recovery/talos.age --source-fenced
```

`--source-fenced` confirms that the source cannot continue operating under the same
installation identity. Restore does not overwrite another installation. The restored
installation invalidates runtime identities and administrator sessions, leaves agents
stopped and channels disabled, and quarantines pending/uncertain deliveries. Reconnect
channels explicitly with a fresh ingress boundary before resuming employee work.

An interrupted restore can be retried with the identical snapshot and destination
host options. Its journal authorizes replacing only the partial restore's owned
resources. Once reopening services begins, retries only finish readiness and release
maintenance; they never reimport an older snapshot over newly admitted work. Retain
the journal and keep the original server fenced throughout recovery.

For an explicit platform update:

```sh
bash talos-release/talos update --version RELEASE_VERSION --archive /absolute/backups/before-update.tar --identity /absolute/recovery/talos.age
```

Or supply an already downloaded target using `--bundle /absolute/release-bundle`.
The target must declare compatibility with the installed release and database revision.
Talos validates/pulls the candidate before maintenance, creates a verified backup,
applies migrations, and verifies readiness. Failure before traffic resumes restores
the prior database, volumes, configuration, and release. It never automatically rolls
back after reopening traffic. Existing employee runtime versions remain pinned;
platform updates do not migrate OpenClaw/Hermes memory or select newer agent versions.

An interrupted maintenance operation remains visible through `status`. Resume that
operation after addressing its error. `status --cancel-maintenance` only cancels a
pre-write operation; it is not a way to bypass a partially written restore/update.
Never delete a maintenance journal or lock to force an operation through.

## Release acceptance infrastructure

`Release candidate` builds both native architectures and uploads a workflow artifact;
it does not publish a release. `Installation acceptance` takes that candidate's run ID.
`Promote preview release` takes both successful workflow-run IDs. All three run
from `main`. Promotion verifies repository ownership, workflow identity, event,
commit, manifest hash, exact image references, required scenarios, and executed JUnit
results without skips. Arbitrary uploaded JSON or a pull-request artifact cannot
authorize promotion. Workflows do not change repository or package visibility.
The workflow environment remains named `private-beta-release`. Configure required
reviewers before promotion and preserve them when opening the repository.

Provision two dedicated disposable SSH hosts for each protected GitHub environment:
`installation-acceptance-amd64` and `installation-acceptance-arm64`. Configure:

- Variables `FIRST_HOST` and `SECOND_HOST`: distinct SSH destinations with the required
  native OS, local Docker, authenticated package access, `uv`, and passwordless reboot.
- Secrets `SSH_KEY`, `SSH_KNOWN_HOSTS`, and `DISPOSABLE_HOST_ID`: the last must match
  `$HOME/.talos-acceptance-host` on both targets, provisioned out of band.
- For amd64, `ACME_DIRECTORY`, `ACME_DOMAIN`, and secret `ACME_ROOT`: a reachable test
  ACME server (for example Pebble), trusted root PEM, and DNS pointing at the first
  host. Configure certificate validity of at most 120 seconds and immediate renewal
  guidance; the test requires an actual changed certificate serial within four minutes.
- For Macs, arrange sign-in and Docker Desktop startup after the fixture reboots.
  The controller waits up to fifteen minutes and fails if Docker does not return.

The controller checks host sentinels before writes, executes the real launcher,
interrupts its management container, reboots the first host, and restores an encrypted
disk-backed backup on the second. It starts actual OpenClaw and Hermes employees in
running, stopped, and uncertain-work states; verifies recovery preserves identities,
does not replay uncertain work, and preserves native volume bytes/Unix permissions;
then stops agents explicitly before backup. A synthetic local candidate crashes its
API to exercise pre-admission update rollback. The prebuilt reliability image runs
the existing fake-provider Telegram/Slack acceptance for both runtimes. There are no
real employee recipients or model-provider credentials.

The same prebuilt image must execute the installer, backup, update, access, and
maintenance tests. The promotion gate checks that the required failure cases ran:
unsupported/unavailable Docker hosts, insufficient resources, occupied ports, registry
credential rejection, interrupted downloads/pulls/migrations/bootstrap, missing backup
resources, foreign ownership, and rollback admission boundaries. These use controlled
fault injection alongside the real-host scenarios above; they are not claims that the
workflow exhausted every possible machine or network failure.

Workflows retain manifests, runtime image identities, logs, and JUnit evidence. Backup
archives and recovery keys are never uploaded with evidence. Failed fixtures are left
on their disposable hosts for investigation; destroy/reprovision those hosts before
the next run. Missing hosts, missing ACME infrastructure, missing credentials, failed
reboot/sign-in, and skipped tests are failures, not evidence of support. These remote
host scenarios must be executed before any beta is promoted.


## Public launch and image access

Source publication and package visibility are separate actions. Before opening any
container package, review every tag and historical version that will become visible,
resolve the license/source obligations in [Third-party notices](../THIRD_PARTY_NOTICES.md),
and require native-host acceptance of the exact prebuilt candidate. Keep the protected
release environment and its reviewer approval. Public package visibility cannot be
reverted to private; do not make experimental package history public unintentionally.

After explicit publication approval, set each required GHCR package to public in its
package settings. Do not assume repository visibility changes package visibility.
From a clean Docker credential configuration, pull every digest in each architecture's
`images-*.txt` and the complete runtime catalog. The promotion gate independently
checks anonymous registry access for every manifest image/runtime reference when the
repository is public, after all existing acceptance checks and before release creation.
A private, missing, or architecture-incompatible image prevents public promotion.

Verify the HTTPS clone, public release downloads, image pulls, and private vulnerability
reporting from an account with no maintainer privileges. Record the source revision,
architecture, exact digests, checksums, and results. A fresh build cache on an existing
Docker Desktop daemon does not establish clean native-host/reboot/restore acceptance.
