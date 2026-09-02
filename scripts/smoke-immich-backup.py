#!/usr/bin/env python3
# ABOUTME: Exercises the encrypted Immich backup round trip with local fixtures.

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


RESTIC_IMAGE = "docker.io/restic/restic:0.19.1@sha256:08916bcda4a4435f9d9828ebb4e91bb7ada3d2c8a53699788930e0ae1bd4fa67"
RCLONE_IMAGE = "docker.io/rclone/rclone:1.74.0@sha256:d2e0e88359d0b2e67cfcd2c43d5405185eb8adfc207079df27c42da82c5207bc"
PLAINTEXT = b"nas-immich-backup-smoke-unique-plaintext-20260830"
XATTR_NAME = "user.nas_backup_smoke"
XATTR_VALUE = b"preserved"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise the encrypted Immich backup round trip."
    )
    parser.add_argument(
        "--runtime",
        choices=("podman", "krun"),
        default="podman",
        help="container runtime to exercise (default: podman/crun)",
    )
    return parser.parse_args()


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def volume(path: Path, destination: str, access: str) -> str:
    return f"{path}:{destination}:{access},Z"


def container_prefix(podman: str, runtime: str) -> list[str]:
    arguments = [podman, "run", "--rm", "--pull=missing"]
    if runtime == "krun":
        # Keep this aligned with nas_backup_run_vm(): the smoke must exercise
        # the same short-lived, resource-bounded libkrun guest as production.
        arguments += [
            "--runtime=krun",
            "--annotation=krun.cpus=2",
            "--annotation=krun.ram_mib=2048",
        ]
    return arguments + [
        "--network=none",
        "--read-only",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        "--cpu-shares=128",
        "--blkio-weight=100",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m",
        "--env=HOME=/tmp",
        "--env=XDG_CACHE_HOME=/tmp/cache",
    ]


def require_complete_snapshot(snapshot_json: str, minimum_bytes: int) -> None:
    try:
        snapshots = json.loads(snapshot_json)
        summary = snapshots[0]["summary"]
        files = int(summary.get("total_files_processed", 0))
        bytes_processed = int(summary.get("total_bytes_processed", 0))
    except (
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
    ) as error:
        raise RuntimeError(
            "restic did not return a usable latest snapshot summary"
        ) from error

    if files <= 0 or bytes_processed < minimum_bytes:
        raise RuntimeError(
            "restic saved an empty or partial snapshot "
            f"(files={files}, bytes={bytes_processed}); "
            f"at least {minimum_bytes} bytes were expected; "
            "this catches the --one-file-system/virtiofs regression"
        )


@contextmanager
def temporary_workspace(podman: str, runtime: str):
    """Create a disposable workspace and restore krun fixture ownership first."""
    temporary = Path(tempfile.mkdtemp(prefix="nas-immich-backup-smoke-"))
    source = temporary / "source"
    try:
        yield temporary
    finally:
        if runtime == "krun" and source.exists():
            # The fixture deliberately uses the NAS service UID in the rootless
            # user namespace. Restore the caller's mapped ownership before
            # cleanup, otherwise TemporaryDirectory-style removal cannot walk
            # the private source after a failed guest invocation.
            restore = subprocess.run(
                [podman, "unshare", "chown", "-R", "0:0", str(source)],
                check=False,
                text=True,
                capture_output=True,
            )
            if restore.returncode != 0:
                print(
                    "failed to restore disposable source ownership: "
                    f"{restore.stderr.strip()}",
                    file=sys.stderr,
                )
        shutil.rmtree(temporary)


def main() -> int:
    arguments = parse_args()
    podman = os.environ.get("CONTAINER_CLI", "podman")
    for image in (RESTIC_IMAGE, RCLONE_IMAGE):
        run([podman, "pull", image])

    with temporary_workspace(podman, arguments.runtime) as temporary:
        root = temporary
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
        nested = source / "upload" / "library" / "nested"
        nested.mkdir(mode=0o750, parents=True)
        nested_original = nested / "nested original.jpg"
        nested_original.write_bytes(PLAINTEXT + b"-nested")
        os.setxattr(nested_original, XATTR_NAME, XATTR_VALUE)
        # Match the production snapshot's private traversal boundary. The
        # backup guest must therefore use only CAP_DAC_READ_SEARCH for photos.
        source.chmod(0o750)
        if arguments.runtime == "krun":
            # Production's snapshot root is owned by Immich (51130:51130),
            # not by the backup runner. podman unshare maps this UID into the
            # disposable rootless namespace while preserving the guest's
            # need for CAP_DAC_READ_SEARCH.
            run([podman, "unshare", "chown", "-R", "51130:51130", str(source)])

        common_restic = container_prefix(podman, arguments.runtime) + [
            "--env=RESTIC_REPOSITORY=/repository",
            "--env=RESTIC_PASSWORD_FILE=/run/secrets/restic-password",
            f"--volume={volume(password, '/run/secrets/restic-password', 'ro')}",
            f"--volume={volume(repository, '/repository', 'rw')}",
            RESTIC_IMAGE,
        ]
        run(common_restic + ["init"])
        run(
            common_restic[:-1]
            + [
                "--cap-add=DAC_READ_SEARCH",
                f"--volume={volume(source, '/source', 'ro')}",
                RESTIC_IMAGE,
                "backup",
                "/source",
                "--host",
                "smoke",
                "--tag",
                "immich",
            ]
        )
        snapshot = subprocess.run(
            common_restic
            + [
                "snapshots",
                "--json",
                "--latest=1",
                "--host=smoke",
                "--path=/source",
                "--tag=immich",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        expected_bytes = len(PLAINTEXT) + len(PLAINTEXT + b"-nested")
        require_complete_snapshot(snapshot.stdout, expected_bytes)
        run(common_restic + ["check", "--read-data"])

        common_rclone = container_prefix(podman, arguments.runtime) + [
            f"--volume={volume(repository, '/repository', 'ro')}",
            f"--volume={volume(remote, '/remote', 'rw')}",
            "--entrypoint=/bin/sh",
            RCLONE_IMAGE,
        ]
        # Keep sync and its acceptance check in one guest, matching the
        # production runner's sequencing and avoiding a second libkrun DHCP/
        # startup boundary between the two operations.
        run(
            common_rclone
            + [
                "-eu",
                "-c",
                "rclone sync /repository /remote --checksum --delete-after; "
                "exec rclone check /repository /remote --checksum",
                "nas-backup-rclone",
            ]
        )

        # Production's object backend can create a temporary restic lock even
        # though lock objects are intentionally excluded from the mirror. A
        # filesystem fixture needs the otherwise-empty prefix materialized.
        (remote / "locks").mkdir()

        remote_restic = container_prefix(podman, arguments.runtime) + [
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
        print(restore.stderr, end="", file=sys.stderr)
        if restore.returncode != 0:
            ignored_errors = [
                line for line in restore.stderr.splitlines() if "ignoring error" in line
            ]
            # Rootless Podman cannot restore the captured SELinux security
            # xattr or chown the production-owned 51130 fixture. The real
            # recovery procedure reapplies host labels and ownership after
            # restore; all other restore errors are fatal.
            if not ignored_errors or any(
                not (
                    "security.selinux: permission denied" in line
                    or "lchown" in line
                    and "operation not permitted" in line
                )
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
        restored_nested = (
            restored
            / "source"
            / "upload"
            / "library"
            / "nested"
            / nested_original.name
        )
        if restored_nested.read_bytes() != PLAINTEXT + b"-nested":
            raise RuntimeError("restored nested file content differs from the source")
        if os.getxattr(restored_nested, XATTR_NAME) != XATTR_VALUE:
            raise RuntimeError("restored nested file xattr differs from the source")
        for repository_file in remote.rglob("*"):
            if repository_file.is_file() and PLAINTEXT in repository_file.read_bytes():
                raise RuntimeError(
                    f"plaintext appeared in encrypted repository object {repository_file}"
                )

    print(
        "Immich backup encryption, mirror, nested data, xattr, and restore "
        f"smoke test passed ({arguments.runtime})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
