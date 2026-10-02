"""Build-time installation of checksum-pinned, official age 1.2.1 binaries."""

import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

CHECKSUMS = {
    "amd64": "7df45a6cc87d4da11cc03a539a7470c15b1041ab2b396af088fe9990f7c79d50",
    "arm64": "57fd79a7ece5fe501f351b9dd51a82fbee1ea8db65a8839db17f5c080245e99f",
}


def main():
    arch = sys.argv[1]
    expected = CHECKSUMS[arch]
    url = f"https://github.com/FiloSottile/age/releases/download/v1.2.1/age-v1.2.1-linux-{arch}.tar.gz"
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read()
    if hashlib.sha256(content).hexdigest() != expected:
        raise SystemExit("age release checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for binary in ("age", "age-keygen"):
            member = archive.getmember(f"age/{binary}")
            if not member.isfile():
                raise SystemExit("age release contains a non-regular binary")
            target = Path("/usr/local/bin") / binary
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(0o755)


if __name__ == "__main__":
    main()
