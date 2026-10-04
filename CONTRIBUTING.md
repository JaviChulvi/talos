# Contributing to Talos

Talos is a development preview. Keep changes focused on an existing owner and
preserve the documented runtime, access, spending, and recovery boundaries.
Discuss substantial behavior or dependency changes in an issue before building.
Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Development

Use Python 3.13, uv 0.11.21, Node.js 22.13 or newer, and pnpm 11.19.0.
From the repository root:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -m 'not integration'
corepack enable
corepack prepare pnpm@11.19.0 --activate
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend lint
pnpm --dir frontend typecheck
pnpm --dir frontend test
pnpm --dir frontend build
```

The ordinary PR checks require no model credentials, employee accounts, or production
services. Docker/PostgreSQL integration and synthetic native-channel checks are
separate; follow [Development and validation](docs/development.md) when changing
runtime adapters, lifecycle, authorization, delivery, or recovery. Installation
acceptance remains required for prebuilt release promotion on both native platforms.
A local unit-test pass is not installation or production acceptance.

## Pull requests

Use a feature branch and describe the problem, resulting behavior, relevant checks,
and any remaining limits. Add a focused regression test for an uncovered behavior
change; documentation-only changes need link/example verification. Keep credentials,
conversation data, screenshots containing private data, and local evidence out of Git.
Use synthetic fixtures. Update the owning guide when a public contract changes.

Do not regenerate dependency locks without an intentional dependency change. For a
dependency or image change, update license provenance and inspect the generated notices
and image inventory described in [Third-party notices](THIRD_PARTY_NOTICES.md).
Preserve upstream copyright and license text. An SBOM does not itself clear
redistribution or satisfy corresponding-source requirements.

CI runs on pull requests with read-only repository permissions and no deployment
secrets. Maintainers may need to approve the first workflow from a new contributor.
Release workflows remain manually dispatched from main in protected environments.

## Contribution licensing

By submitting a contribution, you confirm that you have the right to submit it and
license your original contribution under Talos's [Apache-2.0 license](LICENSE).
Identify copied or adapted material and preserve its original license and notices.
Third-party components are not relicensed by contributing them to this repository.
No separate contributor license agreement is required.
