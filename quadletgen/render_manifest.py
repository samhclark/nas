"""Render machine-readable fleet metadata for non-Python consumers."""

from __future__ import annotations

import posixpath

from .headers import generated_header
from .model import ConfigError, Fleet


def account_units_manifest(fleet: Fleet) -> str:
    lines = [
        generated_header("fleet account units"),
        "# systemd unit",
    ]
    lines.extend(
        sorted(
            f"ensure-nas-{service.host.slug}-account.service"
            for service in fleet.services
        )
    )
    return "\n".join(lines) + "\n"


def active_taps_manifest(fleet: Fleet) -> str:
    lines = [
        generated_header("active TAP services"),
        "# tap\tuser-unit\taccount-unit",
    ]
    for service in fleet.active_taps:
        lines.append(
            _row(
                service.tap_name,
                f"user@{service.host.uid}.service",
                f"ensure-nas-{service.host.slug}-account.service",
            )
        )
    return "\n".join(lines) + "\n"


def host_vm_taps_manifest(fleet: Fleet) -> str:
    lines = [
        generated_header("host VM TAPs"),
        "# name\ttap\tguest\tmanaged-units",
    ]
    for tap in fleet.host_vm_taps:
        lines.append(
            _row(
                tap.name,
                tap.tap_name,
                str(tap.tap_guest),
                ",".join(tap.managed_units),
            )
        )
    return "\n".join(lines) + "\n"


def secrets_manifest(fleet: Fleet) -> str:
    lines = [
        generated_header("fleet secret consumers"),
        "# service\tusername\tsecret",
    ]
    for service in sorted(fleet.services, key=lambda item: item.source.name):
        for secret in service.container.secrets:
            lines.append(
                _row(
                    service.info.name,
                    service.host.username,
                    secret.name,
                )
            )
    if fleet.egress is not None:
        lines.append(_row("mullvad", "root", fleet.egress.secret_name))
    for consumer in sorted(
        fleet.host_secret_consumers,
        key=lambda item: item.name,
    ):
        for secret in consumer.secrets:
            lines.append(_row(consumer.name, "root", secret))
    return "\n".join(lines) + "\n"


def egress_units_manifest(fleet: Fleet) -> str:
    lines = [
        generated_header("fleet egress units"),
        "# systemd unit",
    ]
    if fleet.egress is not None:
        lines.append("nas-egress-mullvad.service")
    return "\n".join(lines) + "\n"


def assets_manifest(fleet: Fleet) -> str:
    lines = [
        generated_header("fleet image assets"),
        "# image asset path",
    ]
    lines.extend(
        sorted(
            {
                service.assets.path
                for service in fleet.services
                if service.assets is not None
            }
        )
    )
    return "\n".join(lines) + "\n"


def storage_units_manifest(fleet: Fleet) -> str:
    lines = [
        generated_header("fleet storage units"),
        "# systemd unit",
    ]
    lines.extend(
        f"nas-prepare-{resource.name}-storage.service"
        for resource in sorted(fleet.resources, key=lambda item: item.name)
    )
    lines.extend(
        f"nas-prepare-{service.info.name}-storage.service"
        for service in sorted(fleet.services, key=lambda item: item.info.name)
        if service.storage
    )
    return "\n".join(lines) + "\n"


def shared_storage_paths_manifest(fleet: Fleet) -> str:
    lines = [
        generated_header("fleet shared storage paths"),
        "# host path; image build installs policy but does not restore labels",
    ]
    for resource in sorted(fleet.resources, key=lambda item: item.name):
        lines.append(resource.host_path)
        lines.extend(
            posixpath.join(resource.host_path, required_path)
            for required_path in resource.required_paths
        )
    return "\n".join(lines) + "\n"


def _row(*fields: str) -> str:
    for field in fields:
        if "\t" in field or "\n" in field:
            raise ConfigError(
                "fleet manifest fields cannot contain tabs or newlines"
            )
    return "\t".join(fields)
