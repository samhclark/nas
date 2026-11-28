#!/usr/bin/env python3
# ABOUTME: Smoke-tests the authored *arr and SABnzbd images under both runtimes.
# ABOUTME: Keeps the libkrun/PID-1 check opt-in and entirely disposable.

"""Opt-in startup smoke tests for the media-automation images.

The runner loads image digests, adapter mounts, and identity declarations from
the authored service TOMLs.  It uses ``/var/tmp`` for all writable mounts,
starts each container with the adapter as PID 1, and succeeds only when the
container stays running for the bounded observation window. The Bazarr krun
case also requires an HTTP response and a clean systemd stop. Failure output
includes the Podman state and logs; cleanup is attempted for every name that
was started.

Both runtime paths request each service's declared mapped in-container
identity, while retaining rootless ``keep-id`` ownership for disposable
mounts. libkrun may still supply guest root to the adapter despite that OCI
request; the image-controlled adapters' guest-root branch is covered by the
entrypoint contract tests. This smoke cannot safely recreate the NAS's
allocated 514xx host UIDs, subordinate ranges, or media GID 52000, and it does
not recreate the production TAP/Mullvad network. Network access is disabled
here. The krun path uses only inspect/logs for observation and never uses
``podman exec``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from quadletgen.model import Service  # noqa: E402
from quadletgen.storage_model import FleetZfsStorage  # noqa: E402
from quadletgen.parser import (  # noqa: E402
    load_fleet,
)


CONTAINER_CLI = os.environ.get("CONTAINER_CLI", "podman")
OVERLAY_ROOT = REPO / "overlay-root"
FLEET_PATH = REPO / "quadlets/_fleet.toml"
SERVICE_NAMES = ("sonarr", "radarr", "prowlarr", "sabnzbd", "bazarr")
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60
DEFAULT_OBSERVATION_SECONDS = 10
POLL_SECONDS = 1
COMMAND_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class RuntimeMode:
    name: str
    runtime: str | None


PODMAN_MODE = RuntimeMode("podman", None)
KRUN_MODE = RuntimeMode("krun", "krun")


def run(
    arguments: Sequence[str],
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



def runtime_probe_arguments(mode: RuntimeMode) -> list[str]:
    arguments = [] if mode.runtime is None else [f"--runtime={mode.runtime}"]
    arguments += ["info", "--format={{.Host.OCIRuntime.Name}}"]
    return arguments


def runtime_available(mode: RuntimeMode) -> bool:
    if mode.runtime is None:
        return True
    result = run(runtime_probe_arguments(mode), capture=True, check=False)
    return result.returncode == 0 and result.stdout.strip() == mode.runtime


def container_name(service_name: str, mode: RuntimeMode) -> str:
    return (
        f"arr-smoke-{service_name}-{mode.name}-{os.getpid()}-"
        f"{uuid.uuid4().hex[:12]}"
    )


def _copy_asset(spec: Service, temporary: Path) -> tuple[Path, str]:
    if spec.assets is None:
        raise RuntimeError(f"{spec.info.name} has no declared image asset")
    asset_path = Path(spec.assets.path)
    if not asset_path.is_absolute():
        raise RuntimeError(f"{spec.info.name} asset path is not absolute")
    try:
        source = (OVERLAY_ROOT / asset_path.relative_to("/")).resolve(strict=True)
    except ValueError as error:
        raise RuntimeError(
            f"{spec.info.name} asset path is outside the overlay root"
        ) from error
    overlay = OVERLAY_ROOT.resolve()
    if source != overlay and overlay not in source.parents:
        raise RuntimeError(f"{spec.info.name} asset escapes overlay-root")

    destination = temporary / "assets" / asset_path.relative_to("/")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)
    return destination, spec.assets.path


def _make_directory(path: Path, mode: str) -> None:
    path.mkdir(mode=int(mode, 8), parents=True, exist_ok=True)
    path.chmod(int(mode, 8))


def _access_suffix(access: object) -> str:
    value = getattr(access, "value", access)
    if value == "read-only":
        return "ro"
    if value == "read-write":
        return "rw"
    raise RuntimeError(f"unsupported storage access: {value!r}")


def _storage_mounts(
    spec: Service,
    temporary: Path,
    resources: Mapping[str, FleetZfsStorage],
) -> list[str]:
    mounts: list[str] = []
    for storage in spec.storage:
        root = temporary / "storage" / spec.info.name / storage.name
        _make_directory(root, getattr(storage, "mode", "0750"))
        for export in storage.exports:
            source = root if export.subpath == "." else root / export.subpath
            source.mkdir(mode=0o750, parents=True, exist_ok=True)
            mounts += [
                "--volume",
                f"{source}:{export.container_path}:"
                f"{_access_suffix(export.access)},Z",
            ]

    for export in spec.shared_storage:
        resource = resources.get(export.resource)
        if resource is None:
            raise RuntimeError(
                f"{spec.info.name} references unknown shared resource "
                f"{export.resource!r}"
            )
        resource_root = temporary / "shared" / export.resource
        for required_path in resource.required_paths:
            required = (
                resource_root
                if required_path == "."
                else resource_root / required_path
            )
            required.mkdir(mode=0o2775, parents=True, exist_ok=True)
        source = (
            resource_root
            if export.subpath == "."
            else resource_root / export.subpath
        )
        source.mkdir(mode=0o2775, parents=True, exist_ok=True)
        mounts += [
            "--volume",
            f"{source}:{export.container_path}:"
            f"{_access_suffix(export.access)},Z",
        ]
    return mounts


def container_arguments(
    spec: Service,
    mode: RuntimeMode,
    name: str,
    temporary: Path,
    resources: Mapping[str, FleetZfsStorage],
) -> list[str]:
    """Build a safe, detached command without launching it."""

    if spec.container.entrypoint is None:
        raise RuntimeError(f"{spec.info.name} has no adapter entrypoint")
    mapped_id = spec.identity.mapped_container_id
    if mapped_id is None or mapped_id <= 0:
        raise RuntimeError(
            f"{spec.info.name} has no positive mapped smoke identity"
        )
    asset_source, asset_target = _copy_asset(spec, temporary)
    process_identity = f"{mapped_id}:{mapped_id}"

    arguments = [] if mode.runtime is None else [f"--runtime={mode.runtime}"]
    arguments += [
        "run",
        "--detach",
        "--name",
        name,
        "--pull=missing",
        "--network=none",
        f"--user={process_identity}",
        f"--userns=keep-id:uid={mapped_id},gid={mapped_id}",
        "--stop-timeout=10",
        f"--entrypoint={spec.container.entrypoint}",
    ]
    if spec.identity.supplemental_groups:
        arguments.append("--group-add=keep-groups")
    if spec.container.no_new_privileges:
        arguments.append("--security-opt=no-new-privileges")
    for capability in spec.container.drop_capabilities:
        arguments.append(f"--cap-drop={capability}")
    for key, value in spec.container.environment:
        arguments += ["--env", f"{key}={value}"]

    for volume in spec.container.volumes:
        if volume.source != asset_target:
            raise RuntimeError(
                f"{spec.info.name} has an unsupported local volume source: "
                f"{volume.source}"
            )
        options = f"{volume.options or 'rw'},Z"
        arguments += [
            "--volume",
            f"{asset_source}:{volume.target}:{options}",
        ]
    arguments += _storage_mounts(spec, temporary, resources)
    arguments.append(spec.container.image)
    return arguments


def cleanup_name(name: str) -> list[str]:
    return ["rm", "--force", "--ignore", name]


def cleanup_container(name: str) -> None:
    try:
        removed = run(
            cleanup_name(name),
            capture=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"cleanup of {name} failed: {error}") from error
    if removed.returncode != 0:
        output = (removed.stdout + removed.stderr).strip()
        raise RuntimeError(
            f"cleanup of {name} failed ({removed.returncode}): "
            f"{output or '<no command output>'}"
        )
    remaining = _inspect(name)
    if remaining.returncode == 0:
        raise RuntimeError(
            f"cleanup of {name} reported success but the container still exists"
        )


def _inspect(name: str) -> subprocess.CompletedProcess[str]:
    return run(
        ["inspect", "--format={{.State.Running}} {{.State.Status}}", name],
        capture=True,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def _failure_details(name: str, reason: str) -> str:
    state = run(
        ["inspect", "--format={{json .State}}", name],
        capture=True,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    logs = run(
        ["logs", "--timestamps", name],
        capture=True,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    state_text = (state.stdout or state.stderr).strip()
    log_text = (logs.stdout + logs.stderr).strip()
    return (
        f"{reason}\ncontainer state: {state_text or '<unavailable>'}\n"
        f"container logs:\n{log_text or '<empty>'}"
    )


def _wait_running(
    name: str,
    *,
    startup_timeout_seconds: int,
    observation_seconds: int,
) -> None:
    deadline = time.monotonic() + startup_timeout_seconds
    while True:
        state = _inspect(name)
        fields = state.stdout.strip().split(maxsplit=1)
        if state.returncode != 0 or not fields:
            raise RuntimeError(_failure_details(name, "container disappeared during startup"))
        if fields[0] == "true":
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                _failure_details(
                    name,
                    f"container did not remain running within "
                    f"{startup_timeout_seconds} seconds",
                )
            )
        time.sleep(POLL_SECONDS)

    observation_deadline = time.monotonic() + observation_seconds
    while time.monotonic() < observation_deadline:
        state = _inspect(name)
        fields = state.stdout.strip().split(maxsplit=1)
        if state.returncode != 0 or not fields or fields[0] != "true":
            raise RuntimeError(
                _failure_details(name, "container exited during observation")
            )
        time.sleep(POLL_SECONDS)


def _smoke_bazarr_stop(spec: Service, name: str, temporary: Path, arguments: list[str]) -> None:
    """Run the adapter under conmon and check systemd's classification of SIGINT."""
    unit = f"{name}.service"
    podman = shutil.which(CONTAINER_CLI)
    assert podman and spec.container.entrypoint
    marker = temporary / "storage/bazarr/config" / name
    port = spec.endpoints_by_name["http"].port
    # Probe loopback inside the network-isolated guest. exec preserves the
    # adapter's process identity and the signal path to Bazarr.
    probe = (
        f"(until curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:{port}/ "
        f"2>/dev/null; do sleep 1; done; touch /config/{marker.name}) & "
        'exec "$0"'
    )
    command = arguments.copy()
    command[command.index(f"--entrypoint={spec.container.entrypoint}")] = "--entrypoint=/bin/sh"
    command[-1:] = [
        "--sdnotify=conmon", "--cgroups=split", "--stop-signal=SIGINT",
        f"--annotation=krun.cpus={spec.krun.cpus}",
        f"--annotation=krun.ram_mib={spec.krun.ram_mib}",
        spec.container.image, "-c", probe, spec.container.entrypoint,
    ]
    systemctl = ["systemctl", "--user"]
    try:
        subprocess.run([
            "systemd-run", "--user", f"--unit={unit}", "--property=Type=notify",
            "--property=NotifyAccess=all", "--property=Delegate=yes", "--property=KillMode=mixed",
            f"--property=SuccessExitStatus={spec.unit.success_exit_status}",
            f"--property=ExecStop={podman} stop {name}", podman, *command,
        ], check=True, timeout=spec.unit.timeout_start_sec)
        deadline = time.monotonic() + spec.krun.probe_timeout_sec
        while not marker.exists():
            if time.monotonic() >= deadline or _inspect(name).stdout.strip() != "true running":
                raise RuntimeError(_failure_details(name, "Bazarr failed its HTTP readiness check"))
            time.sleep(POLL_SECONDS)
        subprocess.run([*systemctl, "stop", unit], check=True, timeout=COMMAND_TIMEOUT_SECONDS)
        result = subprocess.check_output([*systemctl, "show", "-p", "Result", "--value", unit], text=True)
        exit_code = run(["inspect", "--format={{.State.ExitCode}}", name], capture=True).stdout.strip()
        if result.strip() != "success" or exit_code not in ("0", str(spec.unit.success_exit_status)):
            raise RuntimeError(_failure_details(name, f"systemd result {result.strip()}, exit {exit_code}"))
        print(f"Bazarr served HTTP and stopped cleanly (exit {exit_code})", flush=True)
    finally:
        # Successful transient units are automatically unloaded. Clear a failed
        # unit after collecting diagnostics, including on an interrupted smoke.
        subprocess.run([*systemctl, "stop", unit], capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS)
        subprocess.run([*systemctl, "reset-failed", unit], capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS)


def smoke_service(
    spec: Service,
    mode: RuntimeMode,
    temporary: Path,
    resources: Mapping[str, FleetZfsStorage],
    *,
    startup_timeout_seconds: int,
    observation_seconds: int,
) -> None:
    name = container_name(spec.info.name, mode)
    arguments = container_arguments(spec, mode, name, temporary, resources)
    print(
        f"smoke {spec.info.name} [{mode.name}] {spec.container.image}",
        flush=True,
    )
    original_failure: BaseException | None = None
    try:
        if spec.info.name == "bazarr" and mode == KRUN_MODE:
            _smoke_bazarr_stop(spec, name, temporary, arguments)
            return
        try:
            started = run(
                arguments,
                capture=True,
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                _failure_details(name, "podman run timed out")
            ) from error
        if started.returncode != 0:
            output = (started.stdout + started.stderr).strip()
            raise RuntimeError(
                _failure_details(
                    name,
                    f"podman run failed ({started.returncode}): "
                    f"{output or '<no command output>'}",
                )
            )
        _wait_running(
            name,
            startup_timeout_seconds=startup_timeout_seconds,
            observation_seconds=observation_seconds,
        )
    except BaseException as error:
        original_failure = error
        raise
    finally:
        try:
            cleanup_container(name)
        except RuntimeError as cleanup_error:
            if original_failure is None:
                raise
            original_failure.add_note(str(cleanup_error))


def _positive_seconds(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Opt-in smoke tests for the authored *arr images."
    )
    parser.add_argument(
        "--startup-timeout-seconds",
        type=lambda value: _positive_seconds(value, "startup timeout"),
        default=_positive_seconds(
            os.environ.get(
                "ARR_SMOKE_STARTUP_TIMEOUT_SECONDS",
                str(DEFAULT_STARTUP_TIMEOUT_SECONDS),
            ),
            "startup timeout",
        ),
    )
    parser.add_argument(
        "--observation-seconds",
        type=lambda value: _positive_seconds(value, "observation window"),
        default=_positive_seconds(
            os.environ.get(
                "ARR_SMOKE_OBSERVE_SECONDS",
                str(DEFAULT_OBSERVATION_SECONDS),
            ),
            "observation window",
        ),
    )
    return parser.parse_args(argv)


def _interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    modes = [PODMAN_MODE]
    if runtime_available(KRUN_MODE):
        modes.append(KRUN_MODE)
    else:
        print("skip krun: Podman cannot select the krun runtime", flush=True)

    fleet = load_fleet(FLEET_PATH.parent)
    resources = fleet.resources_by_name
    print(
        "limitation: local smoke uses rootless keep-id ownership with the "
        "declared mapped process identity in both ordinary Podman and "
        "krun, disposable storage, and network=none; libkrun may still expose "
        "guest root to the adapter, whose root branch is covered separately. "
        "It does not reproduce NAS service IDs, media GID 52000, TAP/Mullvad "
        "routing, or DNS. krun observation uses no podman exec.",
        flush=True,
    )
    with tempfile.TemporaryDirectory(
        prefix="arr-image-smoke-",
        dir="/var/tmp",
    ) as root:
        temporary = Path(root)
        for mode in modes:
            for service_name in SERVICE_NAMES:
                smoke_service(
                    fleet.services_by_name[service_name],
                    mode,
                    temporary,
                    resources,
                    startup_timeout_seconds=arguments.startup_timeout_seconds,
                    observation_seconds=arguments.observation_seconds,
                )
    print("Arr image smoke tests passed", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Arr image smoke tests interrupted; started containers were cleaned up", file=sys.stderr)
        raise SystemExit(130)
