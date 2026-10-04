# Runtime versions, tools, and connectivity

[← Talos](../README.md) · [Documentation index](../README.md#documentation)

Talos runs pinned OpenClaw and Hermes wrappers with persistent private state. See [source setup](development.md#run-locally) for native UI access and [profile permissions](administration.md#applying-profile-permissions) for tool configuration.

## Runtime version selection

The administrator's **New agent** form includes a version picker for native
OpenClaw and Hermes. **Latest supported** resolves to the highest numeric version
in this installation's approved catalog when the creation request is admitted.
Selecting an explicit version keeps that selection. The API accepts
`runtime_version: "latest"` (also the default when omitted) or a listed version
such as `"2026.9.6"`; requests cannot supply a Docker image. Talos-managed
conversations continue to use the bundled OpenClaw version.

The initial catalog contains the tested OpenClaw **2026.9.6** and Hermes
**0.21.5** builds. It does not automatically follow upstream's newest release.
Additional versions must use Talos's native wrapper and pass runtime acceptance
checks before a host administrator adds them. The Docker build accepts
`OPENCLAW_IMAGE` / `OPENCLAW_VERSION` or `HERMES_IMAGE` / `HERMES_VERSION` build
arguments, checks the upstream package version, and labels the wrapper with
`io.talos.runtime-release`. Use an upstream SHA-256 digest when building. OpenClaw's
browser patch names files in its pinned distribution; a new version may require
updating and retesting that integration before its wrapper will build.

Set `TALOS_RUNTIME_VERSIONS` in `.env` to a JSON object with both `openclaw` and
`hermes` maps, mapping version numbers to installed wrapper image IDs or repository
digests. The default is:

```json
{"openclaw":{"2026.9.6":"talos-openclaw-native:local"},"hermes":{"0.21.5":"talos-hermes-native:local"}}
```

Additional entries require immutable `sha256:…` IDs or `repository@sha256:…`
references, obtained from the wrapper build's `--iidfile` or a registry push. Install
the images on the worker's Docker host and recreate the platform services after
changing the catalog so API and worker share the configuration. Setup targets use
the same catalog and can name multiple versions of one runtime.

At first start the worker verifies the image's release label and saves its local
image ID on the agent. Restarting retains that ID even if a tag moves or the catalog
changes. Migration preserves existing native agents' recorded incarnation images;
legacy bundled images without a release label remain usable. There is no in-place
version change or workspace migration in this picker: upgrade and rollback between
versions require a separately provisioned agent with compatible state. Keep the
images used by existing agents installed.

## Native tools and connectivity

The OpenClaw native image extends the pinned upstream image with `@openclaw/parallel-plugin@2026.9.6`, installed from its lockfile. The `parallel-free` search provider requires no API key; external service availability and limits still apply. Tool calls through a paid model can still cost money. `web_fetch` uses the outbound proxy, and shell/file tools run as the unprivileged container user against the private workspace.

Hermes extends the pinned official image with browser dependencies, with its bundled Parallel key-free search selected at first boot. Search still depends on the external service's availability and limits. Native shell and file tools run as UID 10000 in `/opt/data/workspace`; `/opt/data` persists across starts. No OpenRouter account is required to provision either runtime.

For user Telegram and Slack access managed by Talos, use the [channel setup guide](administration.md#user-and-profile-administration). Additional integrations configured directly inside a runtime use its own plugins and account setup; they are not preconnected and do not inherit Talos user-access checks. Install/configure the required native plugin and supply your own account credentials. HTTP(S) clients must honor the supplied proxy variables or their integration's explicit proxy setting; raw TCP/UDP, inbound webhooks, LAN services, and tools requiring host privileges are not enabled by this setup. Telegram polling and Slack Socket Mode avoid public inbound ports, but channel-specific proxy support and credentials must be checked during setup. No external messages are sent by Talos provisioning.

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

Native instances keep internal Docker bridges. A shared Squid proxy allows public HTTP on port 80 and HTTPS CONNECT on port 443, denying private, loopback, link-local, and reserved destinations after DNS resolution. A separate small TCP relay per instance forwards only to that instance's UI and publishes only on `127.0.0.1`. Neither the relay nor the proxy mounts agent state or credentials. These are container/network boundaries for a trusted local administrator, not isolation between untrusted tenants or protection against all prompt injection. External content can influence an agent with full native tools and its configured credentials.
