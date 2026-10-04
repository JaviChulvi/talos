"""The versioned, immutable release contract shared by packaging and installation."""

import hashlib
import json
import re
import tarfile
from pathlib import Path

from backend.app.runtime_versions import VERSION_PATTERN, validate_catalog

PLATFORMS = ("linux/amd64", "linux/arm64")
IMAGE_ROLES = frozenset(
    (
        "platform",
        "management",
        "verification",
        "egress",
        "openclaw",
        "hermes",
        "postgres",
        "openclaw_base",
        "caddy",
    )
)
IMAGE_PATTERN = r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}"
RELEASE_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9][a-z0-9.-]*)?"


def validate_manifest(value: dict) -> dict:
    """Reject incomplete or unsupported release metadata before touching an installation."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        raise ValueError("Invalid release manifest")
    if value["schema_version"] != 1:
        raise ValueError("Unsupported release manifest format")
    required = {
        "schema_version",
        "version",
        "source_revision",
        "database_revision",
        "images",
        "runtime_versions",
        "compatible_from",
    }
    if set(value) != required:
        raise ValueError("Release manifest fields do not match format 1")
    if not isinstance(value["version"], str) or not re.fullmatch(RELEASE_PATTERN, value["version"]):
        raise ValueError("Release version must be an explicit version")
    if not isinstance(value["source_revision"], str) or not re.fullmatch(
        r"[a-f0-9]{40}", value["source_revision"]
    ):
        raise ValueError("Release source must be a full Git revision")
    if not isinstance(value["database_revision"], str) or not re.fullmatch(
        r"[a-zA-Z0-9_]{1,64}", value["database_revision"]
    ):
        raise ValueError("Release database revision is invalid")
    for field in ("images", "runtime_versions"):
        if not isinstance(value[field], dict) or set(value[field]) != set(PLATFORMS):
            raise ValueError("Release must include both supported container architectures")
    for platform in PLATFORMS:
        images = value["images"][platform]
        if not isinstance(images, dict) or set(images) != IMAGE_ROLES:
            raise ValueError("Release image inventory is incomplete")
        if any(
            not isinstance(ref, str) or not re.fullmatch(IMAGE_PATTERN, ref)
            for ref in images.values()
        ):
            raise ValueError("Release images must be pullable immutable repository digests")
        catalog = value["runtime_versions"][platform]
        if (
            not isinstance(catalog, dict)
            or set(catalog) != {"openclaw", "hermes"}
            or any(not isinstance(versions, dict) for versions in catalog.values())
        ):
            raise ValueError("Release runtime catalog is invalid")
        for family, versions in catalog.items():
            if not versions or any(
                not isinstance(version, str)
                or not re.fullmatch(VERSION_PATTERN, version)
                or not isinstance(ref, str)
                or not re.fullmatch(IMAGE_PATTERN, ref)
                for version, ref in versions.items()
            ):
                raise ValueError("Release runtime catalog requires explicit versions and digests")
            if images[family] not in versions.values():
                raise ValueError("The bundled runtime must appear in the approved catalog")
        validate_catalog(catalog)
    compatibility = value["compatible_from"]
    if not isinstance(compatibility, list):
        raise ValueError("Release compatibility must be a list")
    seen = set()
    for source in compatibility:
        if not isinstance(source, dict) or set(source) != {"version", "database_revision"}:
            raise ValueError("Release compatibility entry is invalid")
        if (
            not isinstance(source["version"], str)
            or not re.fullmatch(RELEASE_PATTERN, source["version"])
            or not isinstance(source["database_revision"], str)
            or not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", source["database_revision"])
        ):
            raise ValueError("Release compatibility entry is invalid")
        if source["version"] in seen or source["version"] == value["version"]:
            raise ValueError("Release compatibility contains duplicate or self references")
        seen.add(source["version"])
    return value


def load_manifest(path: Path) -> dict:
    return validate_manifest(json.loads(path.read_text()))


def verify_bundle(directory: Path) -> dict:
    """Check downloaded bytes; obtain the bundle through the official HTTPS release."""
    entries = {}
    for line in (directory / "checksums.txt").read_text().splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in entries:
            raise ValueError("Invalid release checksums")
        checksum, name = match.groups()
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing release file: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError(f"Release checksum mismatch: {name}")
        entries[name] = checksum
    if (
        not {
            "manifest.json",
            "talos",
            "compose.release.yaml",
            "release.env",
            "images-amd64.txt",
            "images-arm64.txt",
            "LICENSE",
            "NOTICE",
            "THIRD_PARTY_NOTICES.md",
            "license-texts.tar.gz",
            "licenses-amd64.tar.gz",
            "licenses-arm64.tar.gz",
        }
        <= entries.keys()
    ):
        raise ValueError("Release bundle is incomplete")
    manifest = load_manifest(directory / "manifest.json")
    expected_env = []
    for platform in PLATFORMS:
        arch = platform.split("/")[1]
        images = manifest["images"][platform]
        with tarfile.open(directory / f"licenses-{arch}.tar.gz", "r:gz") as archive:
            member = archive.getmember(f"licenses-{arch}/inventory.json")
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError("Invalid image license inventory")
            report = json.load(archive.extractfile(member))
            if (
                report.get("images") != images
                or report.get("platform") != platform
                or report.get("source_revision") != manifest["source_revision"]
                or report.get("scope") != "all-layers"
            ):
                raise ValueError("Image license inventory does not match the release")
            for role in images:
                member = archive.getmember(f"licenses-{arch}/{role}.spdx.json")
                if not member.isfile() or member.size == 0:
                    raise ValueError("Missing image SPDX document")
        expected_env.append(f"TALOS_MANAGEMENT_{arch.upper()}={images['management']}")
        refs = set(images.values())
        for versions in manifest["runtime_versions"][platform].values():
            refs.update(versions.values())
        if set((directory / f"images-{arch}.txt").read_text().splitlines()) != refs:
            raise ValueError("Release pull list does not match the manifest")
    if (directory / "release.env").read_text().splitlines() != expected_env:
        raise ValueError("Release bootstrap images do not match the manifest")
    return manifest
