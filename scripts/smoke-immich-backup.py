#!/usr/bin/env python3
# ABOUTME: Exercises the encrypted Immich backup round trip with local fixtures.

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


RESTIC_IMAGE = "docker.io/restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510"
RCLONE_IMAGE = "docker.io/rclone/rclone:1.74.0@sha256:d2e0e88359d0b2e67cfcd2c43d5405185eb8adfc207079df27c42da82c5207bc"
PLAINTEXT = b"nas-immich-backup-smoke-unique-plaintext-20260830"
XATTR_NAME = "user.nas_backup_smoke"
XATTR_VALUE = b"preserved"


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def volume(path: Path, destination: str, access: str) -> str:
    return f"{path}:{destination}:{access},Z"


def main() -> int:
    podman = os.environ.get("CONTAINER_CLI", "podman")
    for image in (RESTIC_IMAGE, RCLONE_IMAGE):
        run([podman, "pull", image])

    with tempfile.TemporaryDirectory(prefix="nas-immich-backup-smoke-") as temporary:
        root = Path(temporary)
        source = root / "source"
        repository = root / "repository"
        remote = root / "remote"
        restored = root / "restored"
        password = root / "restic-password"
        for directory in (source, repository, remote, restored):
            directory.mkdir()
        password.write_text("local-smoke-only-password\n")
        password.chmod(0o600)

        original = source / "representative original.jpg"
        original.write_bytes(PLAINTEXT)
        os.setxattr(original, XATTR_NAME, XATTR_VALUE)

        common_restic = [
            podman,
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--env=HOME=/tmp",
            "--env=XDG_CACHE_HOME=/tmp/cache",
            "--env=RESTIC_REPOSITORY=/repository",
            "--env=RESTIC_PASSWORD_FILE=/run/secrets/restic-password",
            f"--volume={volume(password, '/run/secrets/restic-password', 'ro')}",
            f"--volume={volume(repository, '/repository', 'rw')}",
            RESTIC_IMAGE,
        ]
        run(common_restic + ["init"])
        run(
            common_restic[:-1]
            + [f"--volume={volume(source, '/source', 'ro')}", RESTIC_IMAGE]
            + ["backup", "/source", "--host", "smoke", "--tag", "immich"]
        )
        run(common_restic + ["check", "--read-data"])

        common_rclone = [
            podman,
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--env=HOME=/tmp",
            "--env=XDG_CACHE_HOME=/tmp/cache",
            f"--volume={volume(repository, '/repository', 'ro')}",
            f"--volume={volume(remote, '/remote', 'rw')}",
            RCLONE_IMAGE,
        ]
        run(common_rclone + ["sync", "/repository", "/remote", "--checksum", "--delete-after"])
        run(common_rclone + ["check", "/repository", "/remote", "--checksum"])

        # Production's object backend can create a temporary restic lock even
        # though lock objects are intentionally excluded from the mirror. A
        # filesystem fixture needs the otherwise-empty prefix materialized.
        (remote / "locks").mkdir()

        remote_restic = [
            podman,
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--env=HOME=/tmp",
            "--env=XDG_CACHE_HOME=/tmp/cache",
            "--env=RESTIC_REPOSITORY=/repository",
            "--env=RESTIC_PASSWORD_FILE=/run/secrets/restic-password",
            f"--volume={volume(password, '/run/secrets/restic-password', 'ro')}",
            f"--volume={volume(remote, '/repository', 'rw')}",
            RESTIC_IMAGE,
        ]
        run(remote_restic + ["check", "--read-data"])
        restore = subprocess.run(
            remote_restic[:-1]
            + [
                "--security-opt=label=disable",
                "--cap-add=MAC_ADMIN",
                f"--volume={volume(restored, '/restore', 'rw')}",
                RESTIC_IMAGE,
            ]
            + ["restore", "latest", "--target", "/restore"],
            text=True,
            capture_output=True,
        )
        print(restore.stdout, end="")
        print(restore.stderr, end="", file=__import__("sys").stderr)
        if restore.returncode != 0:
            ignored_errors = [
                line for line in restore.stderr.splitlines() if "ignoring error" in line
            ]
            # Rootless Podman cannot restore the captured SELinux security
            # xattr. The real recovery procedure intentionally reapplies that
            # host policy with restorecon; all other restore errors are fatal.
            if not ignored_errors or any(
                "security.selinux: permission denied" not in line
                for line in ignored_errors
            ):
                raise subprocess.CalledProcessError(
                    restore.returncode,
                    restore.args,
                    restore.stdout,
                    restore.stderr,
                )

        restored_original = restored / "source" / original.name
        if restored_original.read_bytes() != PLAINTEXT:
            raise RuntimeError("restored file content differs from the source")
        if os.getxattr(restored_original, XATTR_NAME) != XATTR_VALUE:
            raise RuntimeError("restored file xattr differs from the source")
        for repository_file in remote.rglob("*"):
            if repository_file.is_file() and PLAINTEXT in repository_file.read_bytes():
                raise RuntimeError(
                    f"plaintext appeared in encrypted repository object {repository_file}"
                )

    print("Immich backup encryption, mirror, xattr, and restore smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
