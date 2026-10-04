# Third-party notices and redistribution

Talos original code is Copyright 2026 Javier Chulvi Bernad, Apache-2.0. That grant
covers Talos contributions, not the complete contents of a container image.
The following components retain their licenses, copyrights, and applicable
redistribution obligations. Names and logos identify upstream products and do
not imply endorsement or grant trademark rights.

## Distributed components

| Component and version | Origin and immutable identity | Talos changes | License and required disposition |
| --- | --- | --- | --- |
| Python application dependencies | `uv.lock`; installed versions and license texts in `/usr/share/licenses/talos/python-dependencies.json` and `python-notices.txt` | No source edits; wheels include native libraries | Preserve all included notices. Psycopg/psycopg-binary are LGPL-3.0-only; certifi is MPL-2.0. Review corresponding source and replacement/relinking requirements for the exact binary distribution. |
| Frontend dependencies and build tools | `frontend/pnpm-lock.yaml`; served `dependencies.json` and `third-party-notices.txt` | Browser code/CSS is bundled and minified | Preserve package-specific notices alongside browser assets. Inventory conservatively includes build-time dependencies. Missing license texts remain explicit review items. |
| OpenClaw 2026.9.6 | `openclaw/openclaw`, revision `eb377ac59e6c9fd6c7705028034812becf00271b`; `ghcr.io/openclaw/openclaw@sha256:0a5ff5e682e62afa19149df126aa50063bf65ef885b5c94713ce32dc0eb12e15` | `deploy/native/browser-proxy.mjs` patches proxy-routed browser DNS preflight; wrapper adds Chromium and search plugin | MIT for OpenClaw code. Preserve `licenses/openclaw-LICENSE.txt` and `openclaw-THIRD_PARTY_NOTICES.txt`; bundled packages/binaries have separate obligations. |
| Hermes 0.21.5 | `NousResearch/hermes-agent`, revision `749220ef0007f8d87bd1531f1c24b0fe93816385`; `nousresearch/hermes-agent@sha256:d4da4a40cd7a28aba983775d9fd31d94cbf153eeb0cb9e844d6d0f612b7c24db` | No runtime source edits; wrapper adds Chromium, agent-browser and browser-use | MIT for Hermes code; preserve `licenses/hermes-LICENSE.txt`. Image includes other Python, Node, Rust, Go and Debian components; inspect their own notices. |
| `@openclaw/parallel-plugin` 2026.9.6 | `deploy/native/package-lock.json`, npm registry archive/integrity | Installed unchanged | Exact package and transitive license evidence is in the OpenClaw wrapper's SPDX document. Key-free service access is separate from software licensing. |
| `agent-browser` 0.26.0 | npm registry; installed Hermes image inventory | Installed unchanged | Preserve installed notices and inspect transitive/native dependencies in the image inventory. |
| `browser-use[cli]` 0.13.10 | PyPI; installed Hermes image inventory | Installed unchanged | Preserve installed notices; inspect its resolved dependency versions, not only the top-level version. |
| Docker CLI 28.5.2 and Compose 2.40.3 | `docker:28.5.2-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d` | Two unmodified binaries copied into management image | Apache-2.0 for project code. Preserve the separate Docker CLI/Compose LICENSE and NOTICE texts in `licenses/`; embedded Go modules retain their licenses and require review. |
| age 1.2.1 | Official release archives with architecture-specific SHA-256 in `deploy/install_age.py` | Unmodified age and age-keygen binaries | BSD-3-Clause; preserve `licenses/age-LICENSE.txt`. Embedded module notices also apply. |
| Chromium, Squid, OS/base-image packages | Actual image SPDX documents, including Debian source package/version metadata | Package-manager installation; Talos adds proxy configuration | Mixed licenses, including GPL/LGPL. Preserve `/usr/share/doc/*/copyright` and `/usr/share/common-licenses`; satisfy exact corresponding-source obligations before distributing images. |
| Python, Node, uv, PostgreSQL, Caddy and their base images | Immutable pins in `deploy/Dockerfile`, `compose.yaml`, `deploy/build_release.py` and release manifest | Configuration and layering described by Dockerfile; upstream helper images unchanged | Mixed licenses; retain upstream notices, review bundled native code and every distributed layer. Runtime license does not cover its entire base image. |
| Runtime icons | `frontend/public/runtime-icons/` | Distributed as SVG assets | MIT; original license texts remain next to the assets and in frontend build output. |
| Provider icons | `@lobehub/icons-static-svg` 1.95.1, `frontend/public/lab-icons/` | Selected SVG assets | MIT; preserve the bundled `LICENSE.txt`. Provider trademarks remain with their owners. |
| shadcn/ui-derived components | `frontend/src/components/ui/`, shadcn/ui upstream | Adapted for Talos | MIT; retain `licenses/shadcn-ui-LICENSE.txt`. |
| Talos branding and synthetic dashboard screenshot | Talos repository | Original project assets | Apache-2.0 for original copyrightable content; third-party marks retain their rights. |

## Where notices travel

Talos-built image stages carry `LICENSE`, `NOTICE`, this file, and `licenses/`
under `/usr/share/licenses/talos/`. Existing upstream notice files stay in place.
The platform also contains installed Python notices and serves frontend notices
beside the browser assets. Verification regenerates Python notices after adding
test dependencies. Wheels/sdists include the root license and upstream texts.

Prebuilt candidate bundles contain `LICENSE`, `NOTICE`, this document,
`license-texts.tar.gz`, and `licenses-{amd64,arm64}.tar.gz`. Each architecture's
archive contains an SPDX SBOM per image role, a component-level CSV, and an
inventory binding the exact image references to the source commit and platform.
All files are included in `checksums.txt`; the installer retains them. Syft is
checksum-pinned build tooling and is not installed into Talos images.

## Inventory is evidence, not permission

The scanner examines all image layers, including files deleted by later layers.
An SPDX license list may describe distinct files, not a choice of licenses.
Unknown/custom licenses, embedded binaries without provenance, and reciprocal
licenses remain marked for review in `components.csv`; they are not automatically
approved. Source URLs and an SBOM are not substitutes for providing corresponding
source where the applicable license requires it.

Before making any prebuilt image public, resolve every review item against the
exact candidate, retain notices for embedded dependencies, and prepare the required
source archives/build instructions or another applicable compliance mechanism.
Review must cover the entire package history being exposed, not just the newest
tag. The protected release environment's reviewer must inspect this evidence in
addition to native installation acceptance. Do not publish an unresolved candidate.

`licenses/application-inventory.json` is a development dependency observation;
architecture-specific image SBOMs are authoritative for release contents.
`licenses/image-inspection.json` records the inspected arm64 upstream and locally
built wrapper/management/egress images and remaining review counts.
`licenses/observed-image-components.csv.gz` contains their component-level versions,
origins, evidence paths, and provisional dispositions, bound to observed manifest
digests. Decompress it with `gzip -dc licenses/observed-image-components.csv.gz`.
These are development observations, not published-release acceptance or a legal
clearance. A scanner can miss bundled components; exact release scans and review
remain required.
License text download origins and hashes are recorded in `licenses/sources.json`.
