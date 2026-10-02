from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.runtime_versions import DEFAULT_RUNTIME_VERSIONS, resolve_version, runtime_targets


def test_catalog_orders_versions_numerically_and_resolves_specific_pins(monkeypatch):
    catalog = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    catalog["openclaw"].update(
        {"2026.9.10": "sha256:" + "a" * 64, "2026.9.7": "sha256:" + "b" * 64}
    )
    settings = Settings(runtime_versions=catalog)
    monkeypatch.setattr("backend.app.config.get_settings", lambda: settings)
    assert resolve_version("openclaw", "latest") == ("openclaw-2026.9.10", "sha256:" + "a" * 64)
    assert resolve_version("openclaw", "2026.9.7") == ("openclaw-2026.9.7", "sha256:" + "b" * 64)
    assert [
        row["runtime_release"] for row in runtime_targets() if row["runtime_kind"] == "openclaw"
    ] == [
        "openclaw-2026.9.10",
        "openclaw-2026.9.7",
        "openclaw-2026.9.6",
    ]
    with pytest.raises(ValueError, match="supported"):
        resolve_version("openclaw", "2026.9.9")


@pytest.mark.parametrize(
    "version,image",
    [
        ("latest", "sha256:" + "a" * 64),
        ("2026.9.7", "ghcr.io/openclaw/openclaw:latest"),
        ("2026.9.7", "talos-openclaw-native:local"),
    ],
)
def test_catalog_rejects_moving_or_unversioned_additions(version, image):
    catalog = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    catalog["openclaw"][version] = image
    with pytest.raises(ValidationError):
        Settings(runtime_versions=catalog)


def test_setup_can_target_multiple_approved_versions_without_duplicate_targets(monkeypatch):
    from backend.app.setups import BundleError, validate_manifest
    from tests.unit.test_setup_bundles import bundle_fixture

    catalog = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    catalog["hermes"]["0.22.0"] = "sha256:" + "a" * 64
    settings = Settings(runtime_versions=catalog)
    monkeypatch.setattr("backend.app.config.get_settings", lambda: settings)
    manifest, files = bundle_fixture()
    manifest["targets"].append({**manifest["targets"][0], "runtime_release": "hermes-0.22.0"})
    assert len(validate_manifest(manifest, files)["targets"]) == 2
    manifest["targets"].append(dict(manifest["targets"][0]))
    with pytest.raises(BundleError, match="Duplicate"):
        validate_manifest(manifest, files)


def test_saved_setup_survives_removal_of_its_version_from_catalog(tmp_path, monkeypatch):
    from backend.app.setups import (
        BundleError,
        load_bundle,
        store_bundle,
        validate_manifest,
        write_bundle,
    )
    from tests.unit.test_setup_bundles import bundle_fixture

    catalog = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    catalog["hermes"]["0.22.0"] = "sha256:" + "a" * 64
    settings = Settings(runtime_versions=catalog, setup_artifacts_dir=tmp_path)
    monkeypatch.setattr("backend.app.config.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.setups.get_settings", lambda: settings)
    manifest, files = bundle_fixture()
    manifest["targets"][0]["runtime_release"] = "hermes-0.22.0"
    manifest = validate_manifest(manifest, files)
    digest = store_bundle(write_bundle(manifest, files))
    settings.runtime_versions = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    assert load_bundle(digest) == (manifest, files)
    with pytest.raises(BundleError, match="not supported"):
        validate_manifest(manifest, files)
