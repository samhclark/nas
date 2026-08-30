"""Compile a validated fleet into a complete set of filesystem artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .model import ConfigError, Fleet, SUBID_COUNT
from .render_network import (
    network_policy_script,
    network_policy_unit,
    networkmanager_policy,
    networkd_dependencies,
    networkd_netdev,
    networkd_network,
    tailscale_network,
    wireguard_netdev,
    wireguard_network,
    nft_filter,
    nft_nat,
    nftables_policy_dropin,
)
from .render_manifest import (
    account_units_manifest,
    active_taps_manifest,
    assets_manifest,
    egress_units_manifest,
    secrets_manifest,
    shared_storage_paths_manifest,
    storage_units_manifest,
    host_vm_taps_manifest,
)
from .render_fleet import fleet_groups_sysusers_conf
from .render_service import (
    container_unit,
    ensure_account_script,
    ensure_account_unit,
    sysusers_conf,
    tmpfiles_conf,
)
from .render_storage import (
    shared_storage_manifest,
    shared_storage_unit,
    storage_manifest,
    storage_unit,
)
from .render_egress import mullvad_readiness_script, mullvad_readiness_unit
from .render_vector import VECTOR_CONFIG_PATH, vector_config


ARTIFACT_PATH_RE = re.compile(
    r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
)


@dataclass(frozen=True)
class Artifact:
    path: Path
    content: str
    executable: bool = False

    def __post_init__(self) -> None:
        if (
            self.path.is_absolute()
            or not ARTIFACT_PATH_RE.fullmatch(self.path.as_posix())
            or ".." in self.path.parts
        ):
            raise ConfigError(
                f"artifact path must be normalized and relative: {self.path}"
            )
        if type(self.executable) is not bool:
            raise ConfigError("artifact executable flag must be a boolean")


def compile_fleet(fleet: Fleet) -> tuple[Artifact, ...]:
    artifacts: list[Artifact] = []
    for service in fleet.services:
        host = service.host
        if service.container.enabled:
            artifacts.append(
                Artifact(
                    Path(
                        f"etc/containers/systemd/users/{host.uid}/"
                        f"{service.info.name}.container"
                    ),
                    container_unit(service, fleet),
                )
            )
        artifacts += [
            Artifact(
                Path(f"usr/lib/sysusers.d/nas-{host.slug}.conf"),
                sysusers_conf(service),
            ),
            Artifact(
                Path(f"usr/lib/tmpfiles.d/nas-{host.slug}-rootless.conf"),
                tmpfiles_conf(service),
            ),
            Artifact(
                Path(f"usr/local/bin/ensure-nas-{host.slug}-account.sh"),
                ensure_account_script(service, fleet),
                executable=True,
            ),
            Artifact(
                Path(
                    f"etc/systemd/system/ensure-nas-{host.slug}-account.service"
                ),
                ensure_account_unit(service),
            ),
        ]
        if service.active_tap:
            artifacts += [
                Artifact(
                    Path(f"usr/lib/systemd/network/80-{service.tap_name}.netdev"),
                    networkd_netdev(service),
                ),
                Artifact(
                    Path(f"usr/lib/systemd/network/80-{service.tap_name}.network"),
                    networkd_network(service, fleet),
                ),
            ]
        if service.storage:
            artifacts += [
                Artifact(
                    Path(
                        "usr/share/nas/storage/"
                        f"{service.info.name}.storage-manifest"
                    ),
                    storage_manifest(service, fleet),
                ),
                Artifact(
                    Path(
                        "etc/systemd/system/"
                        f"nas-prepare-{service.info.name}-storage.service"
                    ),
                    storage_unit(service),
                ),
            ]

    for tap in fleet.host_vm_taps:
        artifacts += [
            Artifact(
                Path(f"usr/lib/systemd/network/80-{tap.tap_name}.netdev"),
                networkd_netdev(tap),
            ),
            Artifact(
                Path(f"usr/lib/systemd/network/80-{tap.tap_name}.network"),
                networkd_network(tap, fleet),
            ),
        ]
    if fleet.groups:
        artifacts.append(
            Artifact(
                Path("usr/lib/sysusers.d/nas-fleet-groups.conf"),
                fleet_groups_sysusers_conf(fleet),
            )
        )

    for resource in fleet.resources:
        group = fleet.groups_by_name[resource.shared_group]
        artifacts += [
            Artifact(
                Path(
                    "usr/share/nas/storage/"
                    f"{resource.name}.storage-manifest"
                ),
                shared_storage_manifest(resource, group.gid),
            ),
            Artifact(
                Path(
                    "etc/systemd/system/"
                    f"nas-prepare-{resource.name}-storage.service"
                ),
                shared_storage_unit(resource),
            ),
        ]

    subuid = _subid_file(fleet, include_shared_groups=False)
    subgid = _subid_file(fleet, include_shared_groups=True)
    artifacts += [
        Artifact(Path("etc/subuid"), subuid),
        Artifact(Path("etc/subgid"), subgid),
        Artifact(
            Path("usr/local/bin/nas-krun-network-policy.sh"),
            network_policy_script(fleet),
            executable=True,
        ),
        Artifact(
            Path("etc/systemd/system/nas-krun-network-policy.service"),
            network_policy_unit(fleet),
        ),
        Artifact(
            Path("etc/systemd/system/nftables.service.d/10-nas-krun-policy.conf"),
            nftables_policy_dropin(),
        ),
        Artifact(
            Path(
                "etc/systemd/system/systemd-networkd.service.d/"
                "10-nas-krun-accounts.conf"
            ),
            networkd_dependencies(fleet),
        ),
        Artifact(
            Path("etc/NetworkManager/conf.d/90-nas-krun-taps.conf"),
            networkmanager_policy(fleet),
        ),
        Artifact(Path("etc/nftables/nas-krun-filter.nft"), nft_filter(fleet)),
        Artifact(Path("etc/nftables/nas-krun-nat.nft"), nft_nat(fleet)),
        Artifact(
            Path("usr/share/nas/fleet/account-units.list"),
            account_units_manifest(fleet),
        ),
        Artifact(
            Path("usr/share/nas/fleet/egress-units.list"),
            egress_units_manifest(fleet),
        ),
        Artifact(
            Path("usr/share/nas/fleet/active-taps.tsv"),
            active_taps_manifest(fleet),
        ),
        Artifact(
            Path("usr/share/nas/fleet/host-vm-taps.tsv"),
            host_vm_taps_manifest(fleet),
        ),
        Artifact(
            Path("usr/share/nas/fleet/secrets.tsv"),
            secrets_manifest(fleet),
        ),
        Artifact(
            Path("usr/share/nas/fleet/assets.list"),
            assets_manifest(fleet),
        ),
        Artifact(
            Path("usr/share/nas/fleet/storage-units.list"),
            storage_units_manifest(fleet),
        ),
        Artifact(
            Path("usr/share/nas/fleet/shared-storage-paths.list"),
            shared_storage_paths_manifest(fleet),
        ),
        Artifact(Path(VECTOR_CONFIG_PATH), vector_config(fleet)),
    ]
    if fleet.egress is not None:
        artifacts += [
            Artifact(
                Path("usr/lib/systemd/network/60-tailscale0.network"),
                tailscale_network(),
            ),
            Artifact(
                Path("usr/lib/systemd/network/70-wg-arr.netdev"),
                wireguard_netdev(fleet.egress),
            ),
            Artifact(
                Path("usr/lib/systemd/network/70-wg-arr.network"),
                wireguard_network(fleet.egress),
            ),
            Artifact(
                Path("usr/local/bin/nas-egress-mullvad-readiness.sh"),
                mullvad_readiness_script(fleet),
                executable=True,
            ),
            Artifact(
                Path("etc/systemd/system/nas-egress-mullvad.service"),
                mullvad_readiness_unit(),
            ),
        ]
    paths = [artifact.path for artifact in artifacts]
    if len(set(paths)) != len(paths):
        duplicates = sorted(path for path in set(paths) if paths.count(path) > 1)
        raise ConfigError(
            "compiler produced duplicate artifact paths: "
            + ", ".join(map(str, duplicates))
        )
    return tuple(artifacts)


def _subid_file(fleet: Fleet, *, include_shared_groups: bool) -> str:
    # No header comment: shadow-utils does not document comment support in
    # subuid/subgid, so these files stay bare.
    lines = []
    for service in fleet.services:
        lines.append(
            f"{service.host.username}:{service.host.subid_start}:{SUBID_COUNT}"
        )
        if include_shared_groups:
            for group_name in service.identity.supplemental_groups:
                group = fleet.groups_by_name[group_name]
                lines.append(f"{service.host.username}:{group.gid}:1")
    return "\n".join(lines) + "\n"
