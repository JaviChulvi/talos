"""Local loopback access and opt-in Linux domain HTTPS."""

import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml


def validate_domain(domain: str, host_os: str, *, test_acme: bool = False) -> None:
    if host_os != "ubuntu":
        raise ValueError("Domain mode requires the supported Ubuntu host")
    if len(domain) > 253 or not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain
    ):
        raise ValueError(
            "Supply a lowercase fully qualified domain, without a scheme, port or path"
        )
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror:
        raise ValueError(
            "Domain DNS does not resolve; set its A/AAAA records to this server and retry"
        ) from None
    if not addresses or (
        not test_acme and any(not ipaddress.ip_address(ip).is_global for ip in addresses)
    ):
        raise ValueError(
            "Public HTTPS requires publicly routable DNS; inspect every A and AAAA record"
        )


def configure_https(installation, compose: str, values: dict, bundle: Path) -> tuple[str, dict]:
    domain = installation.state["access"]["domain"]
    fixture = installation.read("acceptance-acme.json", {})
    validate_domain(domain, installation.state["host_os"], test_acme=bool(fixture))
    template = (bundle / "Caddyfile.template").read_text()
    options = ""
    volumes = [
        f"{installation.directory}/Caddyfile:/etc/caddy/Caddyfile:ro",
        "caddy-data:/data",
        "caddy-config:/config",
    ]
    if fixture:
        url = urllib.parse.urlsplit(fixture["directory"])
        if (
            url.scheme != "https"
            or not url.hostname
            or any(c.isspace() for c in fixture["directory"])
        ):
            raise ValueError("Acceptance ACME directory must be an HTTPS URL")
        options = (
            f"\n  acme_ca {fixture['directory']}\n  acme_ca_root /etc/caddy/acceptance-root.pem"
        )
        root = installation.directory / "acceptance-acme-root.pem"
        if not root.is_file():
            raise ValueError("Acceptance ACME root certificate is missing")
        volumes.append(f"{root}:/etc/caddy/acceptance-root.pem:ro")
    (installation.directory / "Caddyfile").write_text(
        template.replace("@DOMAIN@", domain).replace("@ACME_OPTIONS@", options)
    )
    values.update(
        TALOS_ALLOWED_HOSTS=json.dumps([domain, "127.0.0.1"]),
        TALOS_ALLOWED_ORIGINS=json.dumps([f"https://{domain}"]),
        TALOS_ADMIN_COOKIE_SECURE="true",
    )
    configuration = yaml.safe_load(compose)
    configuration["services"]["api"].pop("ports", None)
    label = {"io.talos.installation": installation.state["installation_id"]}
    configuration["services"]["caddy"] = {
        "image": values["TALOS_CADDY_IMAGE"],
        "restart": "unless-stopped",
        "labels": label,
        "ports": ["80:80", "443:443"],
        "networks": ["ingress"],
        "volumes": volumes,
        "depends_on": {"api": {"condition": "service_healthy"}},
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "cap_add": ["NET_BIND_SERVICE"],
        "read_only": True,
    }
    for name in ("caddy-data", "caddy-config"):
        configuration["volumes"][name] = {"labels": label}
    return yaml.safe_dump(configuration, sort_keys=False), values


def verify_https(domain: str, *, root: Path | None = None, timeout: int = 180) -> None:
    context = ssl.create_default_context(cafile=str(root) if root else None)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"https://{domain}/health/ready", context=context, timeout=5
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    raise ValueError(
        "HTTPS is not ready. Check DNS, incoming ports 80/443 and Caddy logs, "
        "then rerun install; certificate state is preserved"
    )
