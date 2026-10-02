"""Installation-owned release catalog; agent requests contain versions, never images."""

import re

VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9]+)?"
RUNTIME_RELEASE = "openclaw-2026.9.6"
HERMES_RELEASE = "hermes-0.21.5"
RUNTIME_RELEASES = {"openclaw": RUNTIME_RELEASE, "hermes": HERMES_RELEASE}
DEFAULT_RUNTIME_VERSIONS = {
    "openclaw": {"2026.9.6": "talos-openclaw-native:local"},
    "hermes": {"0.21.5": "talos-hermes-native:local"},
}


def validate_catalog(value):
    if set(value) != set(DEFAULT_RUNTIME_VERSIONS):
        raise ValueError("Configure releases for both OpenClaw and Hermes")
    for kind, versions in value.items():
        if not versions:
            raise ValueError("Each runtime must have at least one approved release")
        for version, image in versions.items():
            if len(version) > 90 or not re.fullmatch(VERSION_PATTERN, version):
                raise ValueError("Runtime versions must be stable numeric releases")
            if len(image) > 255 or (
                DEFAULT_RUNTIME_VERSIONS[kind].get(version) != image
                and not re.fullmatch(r"(?:[a-z0-9][a-z0-9./:_-]*@)?sha256:[a-f0-9]{64}", image)
            ):
                raise ValueError("Additional runtime images must be pinned by SHA-256 digest")
    return value


def versions_for(kind):
    from backend.app.config import get_settings

    return sorted(
        get_settings().runtime_versions[kind],
        key=lambda version: tuple(map(int, re.split(r"[.-]", version))),
        reverse=True,
    )


def resolve_version(kind, version):
    from backend.app.config import get_settings

    version = versions_for(kind)[0] if version == "latest" else version
    image = get_settings().runtime_versions[kind].get(version)
    if image is None:
        raise ValueError("Choose a runtime version supported by this Talos installation")
    return f"{kind}-{version}", image


def runtime_targets():
    return [
        {"runtime_kind": kind, "runtime_release": f"{kind}-{version}"}
        for kind in DEFAULT_RUNTIME_VERSIONS
        for version in versions_for(kind)
    ]
