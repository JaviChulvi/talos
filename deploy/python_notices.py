"""Collect notices from the installed Python distributions, including bundled libraries."""

import importlib.metadata
import json
import sys
from pathlib import Path


def collect(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for distribution in importlib.metadata.distributions():
        metadata = distribution.metadata
        if metadata["Name"] == "talos":
            continue
        notices = []
        for file in distribution.files or ():
            if any(
                word in file.name.lower() for word in ("license", "licence", "notice", "copying")
            ):
                path = Path(distribution.locate_file(file))
                if path.is_file():
                    notices.append({"file": str(file), "text": path.read_text(errors="replace")})
        records.append(
            {
                "name": metadata["Name"],
                "version": distribution.version,
                "origin": metadata.get_all("Project-URL")
                or ([metadata["Home-page"]] if metadata.get("Home-page") else [])
                or [f"https://pypi.org/project/{metadata['Name']}/{distribution.version}/"],
                "license": metadata.get("License-Expression")
                or metadata.get("License")
                or "NOASSERTION",
                "modifications": "Installed from the locked distribution without source edits.",
                "notices": notices,
            }
        )
    records.sort(key=lambda row: row["name"].lower())
    if not records:
        raise ValueError("No installed Python packages were inventoried")
    (destination / "python-dependencies.json").write_text(json.dumps(records, indent=2) + "\n")
    (destination / "python-notices.txt").write_text(
        "\n\n".join(
            f"{row['name']}=={row['version']}\nLicense: {row['license']}\n"
            + "\n".join(f"{notice['file']}\n{notice['text']}" for notice in row["notices"])
            for row in records
        )
    )


if __name__ == "__main__":
    collect(Path(sys.argv[1]))
