"""Real age encryption and Docker volume round-trip proof; run in management image.

TALOS_BACKUP_PROOF_IMAGE selects an already-built disposable helper image. This
checks archive bytes/ownership, not a complete installation or second-host restore.
"""

import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from uuid import uuid4

from backend.management.backup import (
    BackupError,
    _archive_members,
    _decrypt,
    _encrypt,
    _encrypt_command,
    _identity,
)


def main():
    helper = os.environ["TALOS_BACKUP_PROOF_IMAGE"]
    token = uuid4().hex
    source, target = f"talos-backup-proof-{token}-source", f"talos-backup-proof-{token}-target"
    created = []
    try:
        for name in (source, target):
            subprocess.run(
                [
                    "docker",
                    "volume",
                    "create",
                    "--label",
                    f"io.talos.installation=proof-{token}",
                    name,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            created.append(name)
        seed = io.BytesIO()
        with tarfile.open(fileobj=seed, mode="w") as archive:
            entry = tarfile.TarInfo("private")
            entry.type, entry.mode, entry.uid, entry.gid = tarfile.DIRTYPE, 0o700, 10000, 10000
            archive.addfile(entry)
            entry = tarfile.TarInfo("private/state.txt")
            body = b"fake-token-only\nkeep this employee memory"
            entry.mode, entry.uid, entry.gid, entry.size = 0o600, 10000, 10000, len(body)
            archive.addfile(entry, io.BytesIO(body))

        def command(volume, write=False):
            return [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--read-only",
                "--user",
                "0:0",
                "--entrypoint",
                "tar",
                "--mount",
                f"type=volume,src={volume},dst=/data" + ("" if write else ",readonly"),
                helper,
                "--numeric-owner",
                "-xpf" if write else "-cpf",
                "-",
                "-C",
                "/data",
            ]

        subprocess.run(command(source, True), input=seed.getvalue(), check=True)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            identity = _identity(directory / "recovery.key", create=True)
            wrong = _identity(directory / "wrong.key", create=True)
            encrypted = directory / "volume.age"
            _encrypt_command(encrypted, identity, command(source) + ["."])
            assert b"fake-token-only" not in encrypted.read_bytes()
            with _decrypt(encrypted, identity) as stream:
                assert "private/state.txt" in _archive_members(stream)
            try:
                with _decrypt(encrypted, wrong) as stream:
                    stream.read()
            except BackupError:
                pass
            else:
                raise AssertionError("Incorrect recovery key was accepted")
            with _decrypt(encrypted, identity) as stream:
                subprocess.run(command(target, True), stdin=stream, check=True)
            restored = subprocess.run(
                command(target) + ["."], check=True, capture_output=True
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(restored)) as archive:
                item = next(row for row in archive if row.name.endswith("private/state.txt"))
                assert (item.uid, item.gid, item.mode) == (10000, 10000, 0o600)
                assert archive.extractfile(item).read() == body
            truncated = directory / "truncated.age"
            truncated.write_bytes(encrypted.read_bytes()[:-30])
            try:
                with _decrypt(truncated, identity) as stream:
                    stream.read()
            except BackupError:
                pass
            else:
                raise AssertionError("Truncated ciphertext was accepted")
            # Empty streams still receive an authenticated age final chunk.
            _encrypt(directory / "empty.age", identity, io.BytesIO())
            with _decrypt(directory / "empty.age", identity) as stream:
                assert stream.read() == b""
        print(
            "PASS: encrypted Docker volume round-trip, uid/gid/mode preservation, "
            "wrong-key and truncation rejection"
        )
    finally:
        for name in reversed(created):
            subprocess.run(["docker", "volume", "rm", name], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
