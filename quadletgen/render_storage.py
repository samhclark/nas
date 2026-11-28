# ABOUTME: Derives container mounts and startup checks from typed storage contracts.

"""Render the rootless-service side of declarative storage."""

from __future__ import annotations

import posixpath

from .headers import generated_header
from .model import Fleet, SUBID_COUNT, Protocol, Service
from .storage_model import (
    DirectoryStorage,
    FleetZfsStorage,
    ManagedZfsStorage,
    StorageAccess,
    StorageSpec,
)


def storage_volume_lines(service: Service) -> tuple[str, ...]:
    """Return Quadlet comments and mounts derived from service storage."""

    lines: list[str] = []
    for storage in service.storage:
        for export in storage.exports:
            source = _export_path(storage, export.subpath)
            options = ":ro" if export.access is StorageAccess.READ_ONLY else ""
            lines += [
                "",
                f"# Declarative {storage.name} storage",
                f"Volume={source}:{export.container_path}{options}",
            ]
    return tuple(lines)


def shared_storage_volume_lines(
    service: Service,
    fleet: Fleet,
) -> tuple[str, ...]:
    """Render mounts from named, fleet-owned storage exports."""

    lines: list[str] = []
    for export in service.shared_storage:
        resource = fleet.resources_by_name[export.resource]
        source = _fleet_export_path(resource, export.subpath)
        options = ":ro" if export.access is StorageAccess.READ_ONLY else ""
        lines += [
            "",
            f"# Shared fleet resource {resource.name}",
            f"Volume={source}:{export.container_path}{options}",
        ]
    return tuple(lines)


def storage_readiness_line(service: Service) -> str | None:
    """Return the current-boot marker and host-path contract for storage."""

    if not service.storage:
        return None
    requirements: list[str] = []
    seen_paths: set[str] = set()
    for storage in service.storage:
        for export in storage.exports:
            path = _export_path(storage, export.subpath)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            requirements += ["--path", path]
            if isinstance(storage, ManagedZfsStorage):
                requirements += ["--source", storage.dataset]
            requirements += [
                "--owner",
                f"{service.host.uid}:{service.host.uid}",
            ]
            access = (
                "rx"
                if export.access is StorageAccess.READ_ONLY
                else "rwx"
            )
            requirements += ["--access", access]

    marker = f"/run/nas-storage/{service.info.name}/ready"
    return " ".join(
        [
            "ExecStartPre=/usr/local/bin/nas-wait-for-readiness.sh",
            "marker",
            marker,
            "300",
            "2",
            *requirements,
        ]
    )


def shared_storage_readiness_lines(
    service: Service,
    fleet: Fleet,
) -> tuple[str, ...]:
    """Wait on the single fleet preparation marker for each referenced resource."""

    lines: list[str] = []
    for resource_name in dict.fromkeys(
        export.resource for export in service.shared_storage
    ):
        resource = fleet.resources_by_name[resource_name]
        requirements: list[str] = []
        for export in service.shared_storage:
            if export.resource != resource_name:
                continue
            path = _fleet_export_path(resource, export.subpath)
            access = "rx" if export.access is StorageAccess.READ_ONLY else "rwx"
            requirements += [
                "--path",
                path,
                "--source",
                resource.dataset,
                "--access",
                access,
            ]
        lines.append(
            " ".join(
                [
                    "ExecStartPre=/usr/local/bin/nas-wait-for-readiness.sh",
                    "marker",
                    f"/run/nas-storage/{resource.name}/ready",
                    "300",
                    "2",
                    *requirements,
                ]
            )
        )
    return tuple(lines)


def storage_manifest(service: Service, fleet: Fleet | None = None) -> str:
    """Render the closed data language consumed by the storage runtime."""

    if not service.storage:
        raise ValueError(f"{service.info.name} has no storage to render")
    if service.identity.supplemental_groups and fleet is None:
        raise ValueError(
            f"{service.info.name} storage manifest requires fleet groups"
        )
    supplemental_gids = (
        ",".join(
            str(fleet.groups_by_name[name].gid)
            for name in service.identity.supplemental_groups
        )
        if fleet is not None
        else ""
    ) or "-"
    tcp_ports = ",".join(
        str(endpoint.host_port)
        for endpoint in service.container.endpoints
        if endpoint.protocol is Protocol.TCP
        and endpoint.host_port is not None
    ) or "-"
    lines = [
        "nas-storage-manifest-v3",
        generated_header(service.source.name),
        "|".join(
            (
                "service",
                service.info.name,
                service.host.username,
                str(service.host.uid),
                str(service.host.uid),
                str(service.host.subid_start),
                str(SUBID_COUNT),
                tcp_ports,
                supplemental_gids,
            )
        ),
    ]
    emitted_datasets: set[str] = set()
    declared_datasets = {
        item.dataset
        for item in service.storage
        if isinstance(item, ManagedZfsStorage)
    }
    for storage in service.storage:
        if isinstance(storage, DirectoryStorage):
            lines.append(
                f"directory|{storage.host_path}|{storage.mode}"
            )
            for subdirectory in sorted(
                storage.subdirectories,
                key=lambda value: (value.count("/"), value),
            ):
                lines.append(
                    "directory|"
                    f"{posixpath.join(storage.host_path, subdirectory)}|"
                    f"{storage.mode}"
                )
        else:
            parts = storage.dataset.split("/")
            for depth in range(2, len(parts)):
                parent = "/".join(parts[:depth])
                if parent not in emitted_datasets and parent not in declared_datasets:
                    lines.append(f"managed-zfs|{parent}|none|-|-")
                    emitted_datasets.add(parent)
            properties = ",".join(
                (
                    f"recordsize={storage.record_size.value}",
                    f"compression={storage.compression.value}",
                    f"atime={'on' if storage.atime else 'off'}",
                    f"primarycache={storage.primary_cache.value}",
                )
            )
            lines.append(
                f"managed-zfs|{storage.dataset}|{storage.host_path}|"
                f"{storage.mode}|{properties}"
            )
            emitted_datasets.add(storage.dataset)
    return "\n".join(lines) + "\n"


def storage_unit(service: Service) -> str:
    """Render the root one-shot that prepares a service's storage."""

    if not service.storage:
        raise ValueError(f"{service.info.name} has no storage to render")
    account_unit = f"ensure-nas-{service.host.slug}-account.service"
    has_zfs = any(isinstance(item, ManagedZfsStorage) for item in service.storage)
    requires = [account_unit]
    after = ["local-fs.target", account_unit]
    conditions: list[str] = []
    if has_zfs:
        requires.insert(0, "zfs.target")
        after.insert(0, "zfs.target")
        conditions.append("ConditionPathIsDirectory=/sys/module/zfs")

    lines = [
        generated_header(service.source.name),
        "[Unit]",
        f"Description=Prepare declarative storage for {service.info.name}",
        f"Requires={' '.join(requires)}",
        f"After={' '.join(after)}",
        *conditions,
        "",
        "[Service]",
        "Type=oneshot",
        "User=root",
        "ExecStart=/usr/local/bin/nas-prepare-storage.sh "
        f"/usr/share/nas/storage/{service.info.name}.storage-manifest",
        "TimeoutStartSec=infinity",
        "Restart=on-failure",
        "RestartPreventExitStatus=78",
        "RestartSec=30",
        "StandardOutput=journal",
        "StandardError=journal",
        "RemainAfterExit=yes",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return "\n".join(lines) + "\n"


def _export_path(storage: StorageSpec, subpath: str) -> str:
    if subpath == ".":
        return storage.host_path
    return posixpath.join(storage.host_path, subpath)


def shared_storage_manifest(resource: FleetZfsStorage, group_gid: int) -> str:
    """Render the validation-only manifest for one fleet resource."""

    lines = [
        "nas-shared-storage-manifest-v1",
        generated_header("_fleet.toml"),
        "|".join(
            (
                "resource",
                resource.name,
                resource.shared_group,
                str(group_gid),
                str(resource.mode),
                resource.dataset,
                resource.host_path,
            )
        ),
    ]
    lines.extend(f"required|{path}" for path in resource.required_paths)
    return "\n".join(lines) + "\n"


def shared_storage_unit(resource: FleetZfsStorage) -> str:
    """Render the one-shot validator shared by all consumers."""

    return "\n".join(
        [
            generated_header("_fleet.toml"),
            "[Unit]",
            f"Description=Validate fleet storage resource {resource.name}",
            "Requires=zfs.target",
            "After=zfs.target local-fs.target",
            "ConditionPathIsDirectory=/sys/module/zfs",
            "",
            "[Service]",
            "Type=oneshot",
            "User=root",
            "ExecStart=/usr/local/bin/nas-prepare-shared-storage.sh "
            f"/usr/share/nas/storage/{resource.name}.storage-manifest",
            "TimeoutStartSec=infinity",
            "Restart=on-failure",
            "RestartPreventExitStatus=78",
            "RestartSec=30",
            "StandardOutput=journal",
            "StandardError=journal",
            "RemainAfterExit=yes",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _fleet_export_path(storage: FleetZfsStorage, subpath: str) -> str:
    if subpath == ".":
        return storage.host_path
    return posixpath.join(storage.host_path, subpath)
