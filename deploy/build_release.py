"""Build candidate images and assemble the release bundle. Never publish a release."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from backend.app.runtime_versions import DEFAULT_RUNTIME_VERSIONS
from backend.management.release import PLATFORMS, RELEASE_PATTERN, validate_manifest
from deploy.image_inventory import inventory

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "platform": "platform",
    "management": "management",
    "verification": "verification",
    "egress": "egress",
    "openclaw": "native-runtime",
    "hermes": "hermes-runtime",
}
# These are the same upstream pins used by the source installation and helper containers.
UPSTREAM = {
    "postgres": (
        "postgres:17-bookworm@sha256:"
        "639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652"
    ),
    "openclaw_base": (
        "ghcr.io/openclaw/openclaw@sha256:"
        "0a5ff5e682e62afa19149df126aa50063bf65ef885b5c94713ce32dc0eb12e15"
    ),
    "caddy": (
        "caddy:2.10.2-alpine@sha256:"
        "4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d"
    ),
}


def check_upstream_platforms():
    for reference in UPSTREAM.values():
        index = json.loads(
            subprocess.check_output(
                ["docker", "buildx", "imagetools", "inspect", "--raw", reference], text=True
            )
        )
        available = {
            f"{item['platform']['os']}/{item['platform']['architecture']}"
            for item in index.get("manifests", [])
            if "platform" in item
        }
        if not set(PLATFORMS) <= available:
            raise ValueError(f"Upstream image does not support both architectures: {reference}")


def build(platform: str, registry: str, revision: str, output: Path, source_repository: str):
    output.mkdir(parents=True, exist_ok=True)
    native_arch = subprocess.check_output(
        ["docker", "info", "--format", "{{.Architecture}}"], text=True
    ).strip()
    native_arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(native_arch, native_arch)
    if platform != f"linux/{native_arch}":
        raise ValueError("Release builds require a native architecture runner")
    check_upstream_platforms()
    images = dict(UPSTREAM)
    for role, target in TARGETS.items():
        metadata = output / f"{role}.metadata.json"
        repository = f"{registry}-{role}"
        subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                platform,
                "--file",
                "deploy/Dockerfile",
                "--target",
                target,
                "--label",
                f"org.opencontainers.image.revision={revision}",
                "--label",
                f"org.opencontainers.image.source=https://github.com/{source_repository}",
                "--tag",
                f"{repository}:candidate-{revision}-{native_arch}",
                "--metadata-file",
                str(metadata),
                "--push",
                ".",
            ],
            cwd=ROOT,
            check=True,
        )
        digest = json.loads(metadata.read_text())["containerimage.digest"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Build did not produce an immutable image digest")
        images[role] = f"{repository}@{digest}"
    license_inventory = inventory(images, platform, revision, output)
    record = {
        "platform": platform,
        "source_revision": revision,
        "images": images,
        "license_inventory": license_inventory,
    }
    (output / f"images-{native_arch}.json").write_text(json.dumps(record, indent=2) + "\n")


def bundle(
    version: str,
    revision: str,
    database_revision: str,
    images_dir: Path,
    output: Path,
    compatible_from: list,
):
    inventory_archives = []
    manifest = {
        "schema_version": 1,
        "version": version,
        "source_revision": revision,
        "database_revision": database_revision,
        "images": {},
        "runtime_versions": {},
        "compatible_from": compatible_from,
    }
    for platform in PLATFORMS:
        arch = platform.split("/")[1]
        record = json.loads((images_dir / f"images-{arch}.json").read_text())
        if record["source_revision"] != revision or record["platform"] != platform:
            raise ValueError("Candidate images come from a different source or architecture")
        images = record["images"]
        manifest["images"][platform] = images
        evidence = record["license_inventory"]
        archive = images_dir / f"licenses-{arch}.tar.gz"
        if (
            evidence["file"] != archive.name
            or hashlib.sha256(archive.read_bytes()).hexdigest() != evidence["sha256"]
        ):
            raise ValueError("Image license inventory does not match the candidate")
        inventory_archives.append(archive)
        manifest["runtime_versions"][platform] = {
            family: {version: images[family] for version in versions}
            for family, versions in DEFAULT_RUNTIME_VERSIONS.items()
        }
    validate_manifest(manifest)
    # An output directory must be new: never leave stale assets outside the checksum set.
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    assets = [
        ROOT / "deploy/talos",
        ROOT / "deploy/compose.release.yaml",
        ROOT / "LICENSE",
        ROOT / "NOTICE",
        ROOT / "THIRD_PARTY_NOTICES.md",
        *inventory_archives,
    ]
    shutil.make_archive(str(output / "license-texts"), "gztar", ROOT, "licenses")
    assets.extend(sorted((ROOT / "deploy").glob("Caddyfile*")))
    assets.extend(sorted((ROOT / "deploy").glob("compose.https.yaml")))
    for asset in assets:
        shutil.copy2(asset, output / asset.name)
    env = []
    for platform in PLATFORMS:
        arch = platform.split("/")[1]
        images = manifest["images"][platform]
        env.append(f"TALOS_MANAGEMENT_{arch.upper()}={images['management']}")
        refs = set(images.values())
        for versions in manifest["runtime_versions"][platform].values():
            refs.update(versions.values())
        (output / f"images-{arch}.txt").write_text("\n".join(sorted(refs)) + "\n")
    (output / "release.env").write_text("\n".join(env) + "\n")
    checksum_lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(output.iterdir())
        if path.is_file()
    ]
    (output / "checksums.txt").write_text("\n".join(checksum_lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--platform", choices=PLATFORMS, required=True)
    build_parser.add_argument("--registry", required=True)
    build_parser.add_argument("--source-revision", required=True)
    build_parser.add_argument("--source-repository", required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    bundle_parser = commands.add_parser("bundle")
    bundle_parser.add_argument("--version", required=True)
    bundle_parser.add_argument("--source-revision", required=True)
    bundle_parser.add_argument("--database-revision", required=True)
    bundle_parser.add_argument("--images", type=Path, required=True)
    bundle_parser.add_argument("--output", type=Path, required=True)
    bundle_parser.add_argument("--compatible-from", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.source_revision):
        parser.error("source-revision must be a full Git revision")
    if args.command == "build":
        if not re.fullmatch(r"ghcr.io/[a-z0-9._-]+/[a-z0-9._-]+", args.registry):
            parser.error("registry must be a GHCR repository")
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", args.source_repository):
            parser.error("source-repository must be a GitHub owner/repository")
        build(
            args.platform,
            args.registry,
            args.source_revision,
            args.output,
            args.source_repository,
        )
    else:
        if not re.fullmatch(RELEASE_PATTERN, args.version):
            parser.error("version must be an explicit release version")
        compatibility = json.loads(args.compatible_from.read_text()) if args.compatible_from else []
        bundle(
            args.version,
            args.source_revision,
            args.database_revision,
            args.images,
            args.output,
            compatibility,
        )


if __name__ == "__main__":
    main()
