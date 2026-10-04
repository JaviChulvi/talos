"""Access modes expose only the intended public entrypoint and preserve ACME state."""

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
import yaml

from backend.management.access import configure_https, validate_domain
from backend.management.installation import Installation
from deploy.build_release import UPSTREAM

ROOT = Path(__file__).resolve().parents[2]


def dns_answers(*addresses):
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443),
        )
        for address in addresses
    ]


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: dns_answers("8.8.8.8"))
    installation = Installation(tmp_path)
    installation.state = {
        "installation_id": "test-installation",
        "host_os": "ubuntu",
        "access": {"mode": "domain", "domain": "talos.example.com", "port": 8000},
    }
    return installation


def configure(instance):
    return configure_https(
        instance,
        (ROOT / "deploy/compose.release.yaml").read_text(),
        {"TALOS_CADDY_IMAGE": UPSTREAM["caddy"]},
        ROOT / "deploy",
    )


def test_https_exposes_only_caddy_and_persists_owned_certificate_state(instance):
    rendered, values = configure(instance)
    compose = yaml.safe_load(rendered)
    caddy = compose["services"]["caddy"]
    assert caddy["image"] == UPSTREAM["caddy"]
    assert caddy["ports"] == ["80:80", "443:443"]
    assert all(
        "ports" not in service for name, service in compose["services"].items() if name != "caddy"
    )
    assert caddy["networks"] == ["ingress"]
    assert caddy["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert caddy["labels"] == {"io.talos.installation": "test-installation"}
    assert "caddy-data:/data" in caddy["volumes"]
    assert "caddy-config:/config" in caddy["volumes"]
    for name in ("caddy-data", "caddy-config"):
        assert compose["volumes"][name]["labels"] == caddy["labels"]
    assert caddy["read_only"] is True
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["cap_add"] == ["NET_BIND_SERVICE"]
    assert json.loads(values["TALOS_ALLOWED_HOSTS"]) == ["talos.example.com", "127.0.0.1"]
    assert json.loads(values["TALOS_ALLOWED_ORIGINS"]) == ["https://talos.example.com"]
    assert values["TALOS_ADMIN_COOKIE_SECURE"] == "true"
    config = (instance.directory / "Caddyfile").read_text()
    assert "admin off" in config
    assert "reverse_proxy api:8000" in config
    assert "@DOMAIN@" not in config
    assert "acme_ca" not in config
    assert "acceptance-root" not in repr(caddy["volumes"])
    assert configure(instance) == (rendered, values)


@pytest.mark.parametrize(
    "domain",
    [
        "localhost",
        "127.0.0.1",
        "https://talos.example.com",
        "talos.example.com:443",
        "talos.example.com/path",
        "Talos.example.com",
        "talos.example.com\n}",
        "*.example.com",
        "-talos.example.com",
        "talos-.example.com",
        "a" * 64 + ".com",
    ],
)
def test_domain_syntax_rejected_before_dns(domain, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: pytest.fail("Invalid DNS query"))
    with pytest.raises(ValueError, match="lowercase fully qualified domain"):
        validate_domain(domain, "ubuntu")


@pytest.mark.parametrize("host_os", ["macos", "linux", "windows"])
def test_domain_mode_rejects_unsupported_hosts_before_dns(host_os, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: pytest.fail("Unsupported host"))
    with pytest.raises(ValueError, match="supported Ubuntu"):
        validate_domain("talos.example.com", host_os)


@pytest.mark.parametrize(
    "addresses",
    [
        (),
        ("127.0.0.1",),
        ("10.0.0.1",),
        ("::1",),
        ("fd00::1",),
        ("8.8.8.8", "192.168.1.2"),
        ("8.8.8.8", "::1"),
    ],
)
def test_every_dns_record_must_be_public_for_production_https(addresses, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: dns_answers(*addresses))
    with pytest.raises(ValueError, match="publicly routable DNS"):
        validate_domain("talos.example.com", "ubuntu")


def test_both_public_address_families_are_accepted_and_unresolved_dns_is_actionable(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: dns_answers("8.8.8.8", "2001:4860:4860::8888")
    )
    validate_domain("talos.example.com", "ubuntu")

    def unresolved(*args, **kwargs):
        raise socket.gaierror("fixture DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", unresolved)
    with pytest.raises(ValueError, match="DNS does not resolve"):
        validate_domain("talos.example.com", "ubuntu")


def test_custom_ca_is_only_enabled_by_explicit_acceptance_configuration(instance, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: dns_answers("127.0.0.1"))
    with pytest.raises(ValueError, match="publicly routable DNS"):
        configure(instance)
    assert not (instance.directory / "Caddyfile").exists()
    instance.write("acceptance-acme.json", {"directory": "https://acme.test:14000/dir"})
    with pytest.raises(ValueError, match="root certificate is missing"):
        configure(instance)
    root = instance.directory / "acceptance-acme-root.pem"
    root.write_text("Test certificate bytes, validated by Caddy at startup")
    rendered, _ = configure(instance)
    config = (instance.directory / "Caddyfile").read_text()
    assert "acme_ca https://acme.test:14000/dir" in config
    assert "acme_ca_root /etc/caddy/acceptance-root.pem" in config
    assert (
        f"{root}:/etc/caddy/acceptance-root.pem:ro"
        in yaml.safe_load(rendered)["services"]["caddy"]["volumes"]
    )
    assert "admin off" in config


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.test/directory",
        "file:///tmp/acme",
        "https:///directory",
        "https://acme.test/directory\nadmin :2019",
    ],
)
def test_custom_ca_rejects_non_https_and_config_injection(instance, url):
    instance.write("acceptance-acme.json", {"directory": url})
    (instance.directory / "acceptance-acme-root.pem").write_text("test certificate")
    with pytest.raises(ValueError, match="must be an HTTPS URL"):
        configure(instance)
    assert not (instance.directory / "Caddyfile").exists()


def test_launcher_requires_explicit_acceptance_mode_before_custom_ca(tmp_path):
    environment = {
        key: value for key, value in os.environ.items() if key != "TALOS_ACCEPTANCE_TEST"
    }
    result = subprocess.run(
        [
            "/bin/bash",
            str(ROOT / "deploy/talos"),
            "install",
            "--directory",
            str(tmp_path),
            "--acme-directory",
            "https://acme.test/directory",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "ACME overrides are for isolated acceptance fixtures only" in result.stderr
    assert not list(tmp_path.iterdir())
