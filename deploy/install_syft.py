"""Install the checksum-pinned release inventory tool, outside distributed images."""

import hashlib
import io
import platform
import tarfile
import urllib.request
from pathlib import Path

from deploy.image_inventory import SYFT_VERSION

CHECKSUMS = {
    "amd64": "54a87372498168b2d033e876fd41fa4e8035b872699e525a57046e1f2f09c860",
    "arm64": "ee6d4566373a05b344bc6b5f1706f14419bf9338ba39ff686e247deefe9b8818",
}


def main():
    arch = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
    url = (
        f"https://github.com/anchore/syft/releases/download/v{SYFT_VERSION}/"
        f"syft_{SYFT_VERSION}_linux_{arch}.tar.gz"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != CHECKSUMS[arch]:
        raise ValueError("Syft archive checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("syft")
        if not member.isfile():
            raise ValueError("Syft archive does not contain a regular executable")
        destination = Path.home() / ".local/bin/syft"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.extractfile(member).read())
        destination.chmod(0o755)


if __name__ == "__main__":
    main()
