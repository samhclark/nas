#!/usr/bin/env python3
# ABOUTME: Smoke-tests pinned Immich companion images under their declared users.
# ABOUTME: Validates image entrypoints without recreating the production VM topology.

"""Opt-in runtime smoke tests for Immich's PostgreSQL and Valkey images."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from quadletgen.model import Service  # noqa: E402
from quadletgen.parser import load_service  # noqa: E402


CONTAINER_CLI = os.environ.get("CONTAINER_CLI", "podman")
TIMEOUT_SECONDS = 180
COMMAND_TIMEOUT_SECONDS = 300
OVERLAY_ROOT = REPO / "overlay-root"


@dataclass(frozen=True)
class ProcessIdentity:
    uid: int
    gid: int
    label: str


DECLARED_IDENTITY = ProcessIdentity(1000, 1000, "declared 1000:1000")
GUEST_ROOT_IDENTITY = ProcessIdentity(0, 0, "guest-root 0:0 emulation")


def run(
    arguments: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [CONTAINER_CLI, *arguments],
        capture_output=capture,
        check=check,
        text=True,
        timeout=timeout,
    )


def service(name: str) -> Service:
    return load_service(REPO / "quadlets" / f"{name}.toml")


def container_arguments(
    spec: Service,
    name: str,
    *,
    process_identity: ProcessIdentity | None = None,
) -> list[str]:
    container = spec.container
    uid = container.container_user
    if uid is None or uid <= 0:
        raise RuntimeError(f"{spec.info.name} smoke requires a positive container-user")
    identity = process_identity or ProcessIdentity(uid, uid, f"declared {uid}:{uid}")

    arguments = [
        "run",
        "--detach",
        "--name",
        name,
        "--pull=missing",
        "--network=none",
        f"--user={identity.uid}:{identity.gid}",
        f"--userns=keep-id:uid={uid},gid={uid}",
    ]
    if container.no_new_privileges:
        arguments.append("--security-opt=no-new-privileges")
    for capability in container.drop_capabilities:
        arguments.append(f"--cap-drop={capability}")
    if container.shm_size_mib is not None:
        arguments.append(f"--shm-size={container.shm_size_mib}m")
    if container.entrypoint is not None:
        arguments.append(f"--entrypoint={container.entrypoint}")
    return arguments


def materialize_asset(spec: Service, temporary: Path) -> tuple[Path, str]:
    """Copy an image-controlled asset into disposable storage for a bind mount."""

    if spec.assets is None:
        raise RuntimeError(
            f"{spec.info.name} smoke requires a declared image asset"
        )

    asset_path = Path(spec.assets.path)
    if not asset_path.is_absolute():
        raise RuntimeError(f"image asset path is not absolute: {asset_path}")
    source = (OVERLAY_ROOT / asset_path.relative_to("/")).resolve(strict=True)
    overlay_root = OVERLAY_ROOT.resolve()
    if source != overlay_root and overlay_root not in source.parents:
        raise RuntimeError(f"image asset escapes overlay-root: {asset_path}")

    destination = temporary / "image-assets" / asset_path.relative_to("/")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)
    return destination, spec.assets.path


def postgres_data_container_path(spec: Service) -> str:
    """Return the production-declared PostgreSQL storage mount target."""

    storage = tuple(item for item in spec.storage if item.name == "data")
    if len(storage) != 1 or len(storage[0].exports) != 1:
        raise RuntimeError(
            f"{spec.info.name} smoke requires one data storage with one export"
        )
    return storage[0].exports[0].container_path


def ownership_snapshot(path: Path) -> dict[Path, tuple[int, int]]:
    paths = [path]
    if path.is_dir():
        paths.extend(sorted(path.rglob("*")))
    return {
        candidate.relative_to(path): (
            candidate.lstat().st_uid,
            candidate.lstat().st_gid,
        )
        for candidate in paths
    }


def assert_host_ownership(
    path: Path,
    before: dict[Path, tuple[int, int]],
    description: str,
) -> None:
    after = ownership_snapshot(path)
    expected = (os.geteuid(), os.getegid())
    for relative, ownership in after.items():
        if ownership != expected:
            raise RuntimeError(
                f"{description} changed host ownership for {relative}: "
                f"expected {expected[0]}:{expected[1]}, "
                f"got {ownership[0]}:{ownership[1]}"
            )
    for relative, ownership in before.items():
        if after.get(relative) != ownership:
            raise RuntimeError(
                f"{description} changed existing host ownership for {relative}: "
                f"before {ownership[0]}:{ownership[1]}, "
                f"after {after.get(relative)}"
            )


def wait_for_exec(name: str, command: list[str], description: str) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_result: subprocess.CompletedProcess[str] | None = None
    while time.monotonic() < deadline:
        last_result = run(
            ["exec", name, *command],
            capture=True,
            check=False,
            timeout=10,
        )
        if last_result.returncode == 0:
            return
        running = run(
            ["inspect", "--format={{.State.Running}}", name],
            capture=True,
            check=False,
            timeout=10,
        )
        if running.returncode != 0 or running.stdout.strip() != "true":
            break
        time.sleep(1)

    logs = run(["logs", name], capture=True, check=False, timeout=30)
    state = run(
        [
            "inspect",
            "--format={{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}",
            name,
        ],
        capture=True,
        check=False,
        timeout=10,
    )
    detail = ""
    if last_result is not None:
        detail = (last_result.stderr or last_result.stdout).strip()
    raise RuntimeError(
        f"{description} did not become ready: {detail}\n"
        f"container state: {(state.stdout or state.stderr).strip()}\n"
        f"{logs.stdout}{logs.stderr}"
    )


def smoke_postgresql_identity(
    spec: Service,
    temporary: Path,
    asset: tuple[Path, str],
    identity: ProcessIdentity,
) -> None:
    name = (
        f"immich-postgres-smoke-{identity.uid}-{os.getpid()}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    data = temporary / f"postgres-{identity.uid}"
    data.mkdir(mode=0o700)
    before = ownership_snapshot(data)
    environment = dict(spec.container.environment)
    database = environment["POSTGRES_DB"]
    database_user = environment["POSTGRES_USER"]
    password_target = environment["POSTGRES_PASSWORD_FILE"]
    password_file = temporary / f"immich-db-password-{identity.uid}"
    password_file.write_text("immich-smoke-only\n")
    password_file.chmod(0o400)
    arguments = container_arguments(
        spec,
        name,
        process_identity=identity,
    )
    arguments += [
        "--volume",
        f"{data}:{postgres_data_container_path(spec)}:Z",
        "--volume",
        f"{asset[0]}:{asset[1]}:ro,Z",
        "--volume",
        f"{password_file}:{password_target}:ro,Z",
    ]
    for key in (
        "POSTGRES_PASSWORD_FILE",
        "POSTGRES_USER",
        "POSTGRES_DB",
        "POSTGRES_INITDB_ARGS",
        "DB_STORAGE_TYPE",
    ):
        arguments += ["--env", f"{key}={environment[key]}"]
    arguments.append(spec.container.image)
    if spec.container.exec is None:
        raise RuntimeError(f"{spec.info.name} smoke requires an explicit command")
    arguments.extend(spec.container.exec.split())

    print(
        f"smoke PostgreSQL ({identity.label}): {spec.container.image}",
        flush=True,
    )
    failure: Exception | None = None
    try:
        run(arguments, capture=True)
        wait_for_exec(
            name,
            ["pg_isready", "--username", database_user, "--dbname", database],
            "PostgreSQL",
        )
        wait_for_exec(
            name,
            [
                "env",
                "PGPASSWORD=immich-smoke-only",
                "psql",
                "--username",
                database_user,
                "--dbname",
                database,
                "--tuples-only",
                "--no-align",
                "--command=SELECT 1",
            ],
            "PostgreSQL database initialization",
        )
        checksums = run(
            [
                "exec",
                "--env",
                "PGPASSWORD=immich-smoke-only",
                name,
                "psql",
                "--username",
                database_user,
                "--dbname",
                database,
                "--tuples-only",
                "--no-align",
                "--command=SHOW data_checksums",
            ],
            capture=True,
            check=False,
        )
        if checksums.returncode != 0:
            raise RuntimeError(
                "PostgreSQL checksum query failed: "
                f"{(checksums.stderr or checksums.stdout).strip()}"
            )
        if checksums.stdout.strip() != "on":
            raise RuntimeError("PostgreSQL smoke initialized without data checksums")
    except Exception as exc:
        failure = exc
    finally:
        run(["rm", "--force", "--ignore", name], capture=True, timeout=30)
        try:
            assert_host_ownership(data, before, "PostgreSQL smoke data")
        except Exception as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


def smoke_postgresql(spec: Service, temporary: Path) -> None:
    asset = materialize_asset(spec, temporary)
    smoke_postgresql_identity(
        spec,
        temporary,
        asset,
        DECLARED_IDENTITY,
    )
    smoke_postgresql_identity(
        spec,
        temporary,
        asset,
        GUEST_ROOT_IDENTITY,
    )


def smoke_valkey(spec: Service, temporary: Path) -> None:
    name = f"immich-valkey-smoke-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    data = temporary / "valkey"
    data.mkdir(mode=0o750)
    overcommit = temporary / "valkey-overcommit-memory"
    overcommit.write_text("0\n")
    asset = materialize_asset(spec, temporary)
    # Ordinary rootless crun cannot change the host-global overcommit sysctl.
    # Emulate libkrun's observed guest-root identity and give the adapter a
    # disposable file standing in for the guest-private procfs value.
    arguments = container_arguments(
        spec,
        name,
        process_identity=GUEST_ROOT_IDENTITY,
    )
    arguments += [
        "--volume",
        f"{data}:/data:Z",
        "--volume",
        f"{asset[0]}:{asset[1]}:ro,Z",
        "--volume",
        f"{overcommit}:/run/nas-smoke/overcommit-memory:Z",
        "--env",
        "NAS_VALKEY_OVERCOMMIT_PATH=/run/nas-smoke/overcommit-memory",
        spec.container.image,
    ]
    if spec.container.exec is not None:
        arguments.extend(spec.container.exec.split())

    print(f"smoke Valkey: {spec.container.image}", flush=True)
    try:
        run(arguments, capture=True)
        wait_for_exec(name, ["valkey-cli", "ping"], "Valkey")
        if overcommit.read_text().strip() != "1":
            raise RuntimeError("Valkey entrypoint did not enable memory overcommit")
    finally:
        run(["rm", "--force", "--ignore", name], capture=True, timeout=30)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="immich-image-smoke-", dir="/var/tmp") as root:
        temporary = Path(root)
        smoke_postgresql(service("immich-database"), temporary)
        smoke_valkey(service("immich-valkey"), temporary)
    print("Immich companion image smoke tests passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
