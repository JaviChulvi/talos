"""Inventory exact image layers with pinned Syft; unknown licenses remain review items.

Run only on trusted release images. SPDX documents include observed license text;
this does not replace the distributor's corresponding-source obligations.
"""

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

SYFT_VERSION = "1.54.0"


def inventory(images: dict, platform: str, revision: str, destination: Path):
    version = json.loads(subprocess.check_output(["syft", "version", "-o", "json"], text=True))
    if version.get("version") != SYFT_VERSION:
        raise ValueError(f"Image inventory requires Syft {SYFT_VERSION}")
    arch = platform.split("/")[1]
    directory = destination / f"licenses-{arch}"
    directory.mkdir(parents=True, exist_ok=False)
    records = []
    environment = dict(os.environ, SYFT_CHECK_FOR_APP_UPDATE="false", SYFT_LICENSE_CONTENT="all")
    for role, reference in sorted(images.items()):
        raw = directory / f"{role}.syft.json"
        spdx = directory / f"{role}.spdx.json"
        subprocess.run(
            [
                "syft",
                "scan",
                "registry:" + reference,
                "--platform",
                platform,
                "--scope",
                "all-layers",
                "--enrich",
                "javascript,python,golang",
                "-o",
                f"syft-json={raw}",
                "-o",
                f"spdx-json={spdx}",
                "--quiet",
            ],
            env=environment,
            check=True,
        )
        records.extend(component_rows(json.loads(raw.read_text()), role, reference))
        raw.unlink()
    fields = (
        "image_role",
        "image",
        "name",
        "version",
        "type",
        "origin",
        "licenses",
        "evidence_paths",
        "modifications",
        "disposition",
    )
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(records)
    (directory / "components.csv").write_text(stream.getvalue())
    report = {
        "schema_version": 1,
        "source_revision": revision,
        "platform": platform,
        "scanner": f"syft {SYFT_VERSION}",
        "scope": "all-layers",
        "images": images,
        "components": len(records),
        "requires_review": sum(row["disposition"] != "retain_notices" for row in records),
        "status": "requires_review",  # Classification is not legal clearance.
    }
    (directory / "inventory.json").write_text(json.dumps(report, indent=2) + "\n")
    archive = destination / f"licenses-{arch}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(directory, arcname=f"licenses-{arch}")
    return {"file": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}


def component_rows(sbom: dict, role: str, reference: str):
    rows = []
    for package in sbom["artifacts"]:
        expressions = sorted(
            {
                item.get("spdxExpression") or item.get("value") or "NOASSERTION"
                for item in package.get("licenses", [])
            }
        )
        license_text = " ; ".join(expressions) or "NOASSERTION"
        # Each expression can represent a different file; do not turn the list into an OR grant.
        disposition = "retain_notices"
        if not expressions or any(
            "LicenseRef" in item or "NOASSERTION" in item for item in expressions
        ):
            disposition = "license_review_required"
        elif re.search(r"(?:A?GPL|LGPL|MPL|EPL|CDDL|CC-BY-SA)", license_text):
            disposition = "source_or_reciprocity_review_required"
        elif any(
            not re.fullmatch(
                r"(?:MIT(?:-0)?|Apache-2.0|BSD-[234]-Clause|ISC|0BSD|Unlicense|CC0-1.0|"
                r"PSF-2.0|Python-2.0|Zlib|BSL-1.0|PostgreSQL)(?: (?:AND|OR) "
                r"(?:MIT|Apache-2.0|BSD-[234]-Clause))*",
                item,
            )
            for item in expressions
        ):
            disposition = "license_review_required"
        paths = sorted({location["path"] for location in package.get("locations", [])})
        metadata = package.get("metadata") or {}
        rows.append(
            {
                "image_role": role,
                "image": reference,
                "name": package["name"],
                "version": package.get("version", "UNKNOWN"),
                "type": package["type"],
                "origin": package.get("purl") or metadata.get("url") or "UNKNOWN",
                "licenses": license_text,
                "evidence_paths": " ; ".join(paths),
                "modifications": "See THIRD_PARTY_NOTICES.md; compare upstream sources.",
                "disposition": disposition,
            }
        )
    if not rows:
        raise ValueError("Image scan returned no components")
    return rows
