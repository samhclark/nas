"""Render per-service Quadlet and host-account artifacts."""

from __future__ import annotations

import textwrap

from .headers import generated_header
from .model import (
    DependencyCondition,
    Fleet,
    KrunPasst,
    KrunTap,
    Protocol,
    SUBID_COUNT,
    Service,
)
from .render_storage import (
    shared_storage_readiness_lines,
    shared_storage_volume_lines,
    storage_readiness_line,
    storage_volume_lines,
)


def header(service: Service) -> str:
    return generated_header(service.source.name)


def wrap_comment(text: str) -> str:
    return textwrap.fill(
        text,
        width=78,
        initial_indent="# ",
        subsequent_indent="# ",
    )


def container_unit(service: Service, fleet: Fleet) -> str:
    info = service.info
    container = service.container
    krun = service.krun
    lines = [header(service)]
    lines += ["[Unit]", f"Description={info.description}"]
    if info.documentation is not None:
        lines.append(f"Documentation={info.documentation}")

    lines += ["", "[Container]"]
    lines.append(f"ContainerName={info.name}")
    lines.append(f"Image={container.image}")
    if container.log_driver is not None:
        lines.append(f"LogDriver={container.log_driver}")
    if container.entrypoint is not None:
        lines.append(f"Entrypoint={container.entrypoint}")
    if krun is not None:
        lines.append("PodmanArgs=--runtime=krun")
        lines.append(f"Annotation=krun.cpus={krun.cpus}")
        lines.append(f"Annotation=krun.ram_mib={krun.ram_mib}")
        if isinstance(krun, KrunPasst):
            lines.append("Annotation=krun.use_passt=1")
        elif isinstance(krun, KrunTap):
            lines.append(f"Annotation=krun.tap_name={service.tap_name}")
            if krun.guest_bootstrap_root:
                lines.append("Annotation=krun.guest_bootstrap_root=1")
        lines.append("StopSignal=SIGINT")
    if container.network is not None:
        lines.append(f"Network={container.network}")
    if service.active_tap:
        # libkrun opens /dev/net/tun after crun enters the container mount
        # namespace, so the device must exist there as well as on the host.
        lines.append("AddDevice=/dev/net/tun")
        for peer in fleet.active_taps:
            lines.append(f"AddHost={peer.info.name}.krun:{peer.tap_guest.ip}")
        lines.append(f"AddHost=host.krun.internal:{service.tap_gateway.ip}")
    for server in container.dns:
        lines.append(f"DNS={server}")
    for setting in container.sysctls:
        lines.append(f"Sysctl={setting}")
    if container.container_user is not None:
        user = (
            f"{container.container_user}:{container.container_user}"
            if container.container_user > 0
            else str(container.container_user)
        )
        lines.append(f"User={user}")
        if (
            container.container_user > 0
            and service.identity.mapped_container_id is None
        ):
            lines.append(
                "UserNS=keep-id:"
                f"uid={container.container_user},gid={container.container_user}"
            )
    if service.identity.mapped_container_id is not None:
        mapped_id = service.identity.mapped_container_id
        mapped_host_gid = (
            fleet.groups_by_name[service.identity.mapped_group].gid
            if service.identity.mapped_group is not None
            else service.host.uid
        )
        lines.append(f"UIDMap=+u{mapped_id}:@{service.host.uid}:1")
        lines.append(f"GIDMap=+g{mapped_id}:@{mapped_host_gid}:1")
    if service.identity.supplemental_groups:
        lines.append("GroupAdd=keep-groups")
    if container.health_cmd is not None:
        lines.append(f"HealthCmd={container.health_cmd}")
    if container.no_new_privileges:
        lines.append("NoNewPrivileges=true")
    for capability in container.drop_capabilities:
        lines.append(f"DropCapability={capability}")
    if container.shm_size_mib is not None:
        lines.append(f"ShmSize={container.shm_size_mib}m")

    if container.environment:
        lines.append("")
        lines += [
            f"Environment={key}={value}"
            for key, value in container.environment
        ]

    for volume in container.volumes:
        lines.append("")
        if volume.comment is not None:
            lines.append(f"# {volume.comment}")
        options = f":{volume.options}" if volume.options else ""
        lines.append(f"Volume={volume.source}:{volume.target}{options}")

    lines += storage_volume_lines(service)
    lines += shared_storage_volume_lines(service, fleet)

    for secret in container.secrets:
        target = secret.target or f"/run/secrets/{secret.name}"
        lines.append("")
        lines.append(
            "# Runtime secret written at boot by "
            "sops-distribute-secrets.service"
        )
        lines.append(
            f"Volume=/run/nas-secrets/{info.name}/{secret.name}:"
            f"{target}:ro,Z"
        )

    published_endpoints = tuple(
        endpoint
        for endpoint in container.endpoints
        if endpoint.publication is not None
    )
    if published_endpoints and not isinstance(krun, KrunTap):
        lines.append("")
        for endpoint in published_endpoints:
            suffix = (
                ""
                if endpoint.protocol is Protocol.TCP
                else f"/{endpoint.protocol.value}"
            )
            lines.append(
                f"PublishPort={endpoint.publication}:{endpoint.port}{suffix}"
            )

    if container.exec is not None:
        lines += ["", f"Exec={container.exec}"]

    lines += ["", "[Service]"]
    if service.active_tap:
        lines.append("ExecStartPre=/usr/bin/test -c /dev/net/tun")
        lines.append(
            "ExecStartPre=/usr/bin/bash -ceu '"
            "for i in {1..90}; do "
            "if /usr/bin/test -r /run/nas-krun-network/policy-ready && "
            f"/usr/bin/test -e /sys/class/net/{service.tap_name}; then exit 0; fi; "
            "sleep 1; done; "
            'echo "krun network policy was not ready within 90 seconds" >&2; exit 1'"'"
        )
        if getattr(krun, "egress", None) == "mullvad":
            lines.append(
                "ExecStartPre=/usr/local/bin/nas-wait-for-readiness.sh "
                "marker /run/nas-egress/mullvad/ready 60 1"
            )
    lines += [
        f"ExecStartPre=/usr/bin/test -r /run/nas-secrets/"
        f"{info.name}/{secret.name}"
        for secret in container.secrets
    ]
    storage_readiness = storage_readiness_line(service)
    if storage_readiness is not None:
        lines.append(storage_readiness)
    lines += shared_storage_readiness_lines(service, fleet)
    for dependency in service.startup.dependencies:
        target_service = fleet.services_by_name[dependency.service]
        target_endpoint = target_service.endpoints_by_name[dependency.endpoint]
        if dependency.condition is DependencyCondition.HTTP:
            target = (
                f"http://{target_service.tap_guest.ip}:"
                f"{target_endpoint.port}{dependency.path}"
            )
        else:
            target = f"{target_service.tap_guest.ip}:{target_endpoint.port}"
        lines.append(
            "ExecStartPre=/usr/local/bin/nas-wait-for-readiness.sh "
            f"{dependency.condition.value} {target} {dependency.timeout_sec} "
            f"{dependency.interval_sec}"
        )
    if service.startup.require_published_tcp_ports_free:
        host_ports = " ".join(
            str(endpoint.host_port)
            for endpoint in container.endpoints
            if endpoint.protocol is Protocol.TCP
            and endpoint.host_port is not None
        )
        lines.append(
            "ExecStartPre=/usr/local/bin/nas-assert-tcp-ports-free.sh "
            f"{host_ports}"
        )
    if service.active_tap:
        probe = service.endpoints_by_name[service.tap_spec.probe_endpoint]
        lines.append(
            "ExecStartPost=/usr/bin/bash -ceu '"
            f"for i in {{1..{service.tap_spec.probe_timeout_sec}}}; do "
            f"if /usr/bin/timeout 1 /usr/bin/bash -c \"</dev/tcp/{service.tap_guest.ip}/{probe.port}\" "
            ">/dev/null 2>&1; "
            "then exit 0; fi; sleep 1; done; "
            f"echo \"libkrun guest {service.tap_guest.ip}:{probe.port} was not reachable\" >&2; "
            "exit 1'"
        )
    lines.append("Restart=always")
    lines.append(f"RestartSec={service.unit.restart_sec}")
    timeout_start_sec = service.unit.timeout_start_sec
    if timeout_start_sec is None and (service.storage or service.shared_storage):
        timeout_start_sec = 330
    if timeout_start_sec is not None:
        lines.append(f"TimeoutStartSec={timeout_start_sec}")

    lines += ["", "[Install]", "WantedBy=default.target"]
    return "\n".join(lines) + "\n"


def sysusers_conf(service: Service) -> str:
    host = service.host
    comment = wrap_comment(
        f"Host account for the rootless {host.display_name} Quadlet. This must "
        "stay usable by systemd's lingering user manager, so do not mark it "
        'as a fully locked "u!" account.'
    )
    return "\n".join(
        [
            header(service),
            comment,
            f"g {host.username} {host.uid}",
            f'u {host.username} {host.uid}:{host.uid} "NAS '
            f'{host.display_name} service user" /var/home/{host.username} '
            "/sbin/nologin",
        ]
    ) + "\n"


def tmpfiles_conf(service: Service) -> str:
    host = service.host
    lines = [
        header(service),
        wrap_comment(
            f"Rootless {host.display_name} uses a namespaced host account so "
            "the service identity does not collide with upstream image defaults."
        ),
    ]
    home = f"/var/home/{host.username}"
    for subdir in (
        "",
        "/.config",
        "/.config/containers",
        "/.local",
        "/.local/share",
        "/.local/share/containers",
        "/.cache",
    ):
        lines.append(f"d {home}{subdir} 0750 {host.username} {host.username} -")
    lines.append("d /var/lib/systemd/linger 0755 root root -")
    lines.append(f"f /var/lib/systemd/linger/{host.username} 0644 root root -")
    return "\n".join(lines) + "\n"


def ensure_account_script(service: Service, fleet: Fleet) -> str:
    host = service.host
    supplemental_subgid_lines = "\n".join(
        f'ensure_exact_subid_entry /etc/subgid "{fleet.groups_by_name[name].gid}" "1"'
        for name in service.identity.supplemental_groups
    )

    return f"""#!/bin/bash
{header(service)}
# ABOUTME: Ensures the {host.username} rootless service account has the state
# required for a lingering user manager.

set -euo pipefail

USER_NAME="{host.username}"
USER_UID="{host.uid}"
USER_HOME="/var/home/{host.username}"
USER_SHELL="/sbin/nologin"
USER_SUBID_START="{host.subid_start}"
USER_SUBID_COUNT="{SUBID_COUNT}"

log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}}

ensure_primary_subid_entry() {{
    local file="$1"
    local expected="${{USER_NAME}}:${{USER_SUBID_START}}:${{USER_SUBID_COUNT}}"
    local current

    if [[ ! -e "${{file}}" ]]; then
        install -o root -g root -m 0644 /dev/null "${{file}}"
    fi

    current="$(grep -E "^${{USER_NAME}}:" "${{file}}" || true)"

    if [[ -z "${{current}}" ]]; then
        log "Adding ${{expected}} to ${{file}}"
        printf '%s\\n' "${{expected}}" >> "${{file}}"
        return
    fi

    if ! grep -Fqx "${{expected}}" "${{file}}"; then
        log "Leaving existing ${{file}} entry for ${{USER_NAME}}: ${{current}}"
    fi
}}

ensure_exact_subid_entry() {{
    local file="$1"
    local start="$2"
    local count="$3"
    local expected="${{USER_NAME}}:${{start}}:${{count}}"

    if ! grep -Fqx "${{expected}}" "${{file}}"; then
        log "Adding ${{expected}} to ${{file}}"
        printf '%s\\n' "${{expected}}" >> "${{file}}"
    fi
}}

if ! getent passwd "${{USER_NAME}}" >/dev/null; then
    log "User ${{USER_NAME}} does not exist yet, skipping"
    exit 0
fi

shadow_entry="$(getent shadow "${{USER_NAME}}" || true)"
shadow_password_field="${{shadow_entry#*:}}"
shadow_password_field="${{shadow_password_field%%:*}}"

if [[ -z "${{shadow_password_field}}" || "${{shadow_password_field}}" == "!"* ]]; then
    log "Resetting ${{USER_NAME}} to an invalid but not fully locked password marker"
    usermod --password '*' "${{USER_NAME}}"
fi

log "Clearing account expiry for ${{USER_NAME}}"
chage --expiredate -1 "${{USER_NAME}}"

current_home="$(getent passwd "${{USER_NAME}}" | cut -d: -f6)"
if [[ "${{current_home}}" != "${{USER_HOME}}" ]]; then
    log "Resetting home for ${{USER_NAME}} to ${{USER_HOME}}"
    usermod --home "${{USER_HOME}}" "${{USER_NAME}}"
fi

current_shell="$(getent passwd "${{USER_NAME}}" | cut -d: -f7)"
if [[ "${{current_shell}}" != "${{USER_SHELL}}" ]]; then
    log "Resetting shell for ${{USER_NAME}} to ${{USER_SHELL}}"
    usermod --shell "${{USER_SHELL}}" "${{USER_NAME}}"
fi

ensure_primary_subid_entry /etc/subuid
ensure_primary_subid_entry /etc/subgid
{supplemental_subgid_lines}

if systemctl is-failed --quiet "user@${{USER_UID}}.service"; then
    log "Retrying user@${{USER_UID}}.service after account setup"
    systemctl reset-failed "user@${{USER_UID}}.service"
    systemctl start "user@${{USER_UID}}.service"
fi
"""


def ensure_account_unit(service: Service) -> str:
    host = service.host
    return f"""{header(service)}
[Unit]
Description=Prepare {host.username} for a lingering user manager
DefaultDependencies=no
After=local-fs.target systemd-sysusers.service systemd-tmpfiles-setup.service
Before=sysinit.target systemd-logind.service user@{host.uid}.service
ConditionPathExists=/etc/passwd

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ensure-nas-{host.slug}-account.sh
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
"""
