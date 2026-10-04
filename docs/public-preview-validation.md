# Public preview preparation: local validation

Observed on 2026-10-04. This records development evidence for PRs #68–#70;
it is not the protected native-host acceptance required for a prebuilt release.

## Source and environment

The complete synthetic reliability run used clean commit
`37fb573087e5674ec133f89f3e11716a67652d28` (`dirty: false`, exit code 0).
Subsequent changes record inventory evidence and this report; they do not change
application/runtime behavior. Container image IDs from that run:

| Role | Local immutable build ID |
| --- | --- |
| runner | `sha256:6671fad4ea20303651b16ff649870dcf25407daca141f39551f0a243655681ca` |
| openclaw | `sha256:14de9a410aae6572e817ed2883c0afc593766d18670d638312de715e5b29c96f` |
| hermes | `sha256:4f4f27d061ef401e61b9be73e6bd3a08b65b9387a9e4f04dc6dac1a4291850f3` |

JUnit SHA-256: `e0be9be05acd485d192c22ebe224fccbcc5f13e97d8a500a50dabc5d958b4c63`.
The command was `uv run python -m tests.reliability --report REPORT_DIRECTORY`.
The runner records the source revision, image IDs, PostgreSQL digest, test selection,
JUnit and logs. These local outputs are diagnostic evidence; they cannot authorize
release promotion through the workflow-bound release gate.

| Tool or host | Observed version |
| --- | --- |
| Host | Apple Silicon arm64, macOS 27.0 |
| Docker client/server | 27.3.1; Linux aarch64 VM, 11 CPUs, about 7.65 GiB RAM |
| Host Python / uv | 3.13.9 / 0.11.21 |
| Host Node / pnpm | 22.22.0 / 11.19.0 |
| Runtime releases | OpenClaw 2026.9.6; Hermes 0.21.5 |
| Image inventory / secret scanner | Syft 1.54.0 / Gitleaks 8.30.1 |

Exact upstream image references live in `deploy/Dockerfile`, `compose.yaml`,
`deploy/runtimes/openclaw.json` and `deploy/build_release.py`. Installed component
versions and observed image manifest/config identities are recorded in
[`licenses/image-inspection.json`](../licenses/image-inspection.json) and
[`licenses/observed-image-components.csv.gz`](../licenses/observed-image-components.csv.gz).
Apt and browser-tool transitive resolution can change over time; this does not claim
bit-for-bit repeatability. Release SBOMs must describe the actual resulting images.

## Executed checks

| Check | Result and boundary |
| --- | --- |
| Python unit suite | 382 passed; integration tests excluded from this invocation. |
| Native reliability suite | 630 passed, zero skipped; both runtimes, synthetic Slack/Telegram conversations and delivery receipts, setup reproduction, persistence and recovery faults. Two dependency warnings (FastAPI/httpx deprecation and urllib3 response cleanup); exit code 0. |
| Frontend | Lint, TypeScript, six tests and production build passed locally and in PR CI. |
| Packaging | Python wheel/sdist include license texts; release assembly/backup tests preserve the checksummed notices and SBOM archives. Built Talos image targets contain `/usr/share/licenses/talos/`; platform browser assets contain the collected notices. |
| Anonymous source dependencies | A dedicated BuildKit builder with a fresh cache and empty Docker credential config built verification, management, both native wrappers and Squid using `--no-cache --pull`. Platform also built and served the application. Registry scans downloaded upstream helper images anonymously. |
| Compose source smoke | New project and disk-backed volumes: migrations, readiness, interactive admin bootstrap, login, role/employee creation, stop/start and persistence all passed. Synthetic credentials only; owned resources were removed afterward. |
| History secret scan | All reachable fetched history scanned with redacted Gitleaks output: 198 commits, no leaks after two exact historical false-positive fingerprints for a private-key type annotation. This is not an exhaustive private-material or intellectual-property review. |
| PR CI | Read-only Python, frontend and secret-scanning jobs pass without production secrets. |

The first native run found a missing `/app/licenses` directory in the verification
image's release-assembly fixture (629 passed, one failed). Including the source
license files in that stage fixed it; the clean-commit run above passed all 630.

The first Compose attempt hit this workstation's exhausted automatic Docker subnet
pool. An isolated override assigned four unused `10.251.10.0/24`–`10.251.13.0/24`
subnets; no existing networks were removed. A cold Hermes build also exceeded this
workstation's remaining Docker disk space. Removing only the task's dedicated
BuildKit cache allowed the retry to pass. These workstation adjustments do not
establish the documented minimum-resource acceptance on a fresh host.

## Still required before public prebuilt publication

- Resolve every unknown/custom license and reciprocal/corresponding-source item
  against the exact candidate, including embedded/native dependencies. Supply required
  notices and source materials. The observed scanner inventory is not legal clearance.
- Provision the two native disposable hosts per architecture and protected review
  environments. Execute clean installation, reboot, encrypted second-host restore,
  rollback and Linux ACME acceptance for the exact published candidate images.
- Once publication is authorized, configure package visibility explicitly and verify
  full anonymous pulls of every required digest on the clean hosts. The public
  promotion guard checks anonymous manifest access and platform availability; it
  does not replace the full-pull test.
- Verify anonymous HTTPS cloning once the currently private repository is opened.
  Enable GitHub Private Vulnerability Reporting at launch and verify a separate
  researcher's private submission reaches the maintainer's notifications.

No repository/package visibility was changed and no release was published by these
checks. Real providers and employee channels still require the operator's own keys,
accounts and recipients; all executed conversation/delivery checks used fixtures.
