"""Strict TOML parsing for rootless service configuration."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Literal, Mapping, NoReturn

from .model import (
    AssetsSpec,
    ConfigError,
    ContainerSpec,
    Dependency,
    DependencyCondition,
    Endpoint,
    FleetGroup,
    HostSecretConsumer,
    HostVmTap,
    HostIdentity,
    KrunNetwork,
    KrunPasst,
    KrunSpec,
    KrunTap,
    KrunTsi,
    MullvadEgress,
    Protocol,
    SecretMount,
    Service,
    ServiceIdentity,
    ServiceInfo,
    StartupSpec,
    UnitSpec,
    VolumeMount,
)
from .storage_parser import parse_fleet_storage, parse_shared_storage, parse_storage


def _fail(path: str, message: str) -> NoReturn:
    raise ConfigError(f"{path}: {message}")


def _table(
    value: object,
    path: str,
    allowed: set[str],
    *,
    required: bool = True,
) -> dict[str, object]:
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        _fail(path, "must be a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(path, f"has unknown keys: {', '.join(unknown)}")
    return value


def _required(table: Mapping[str, object], key: str, path: str) -> object:
    if key not in table:
        _fail(path, f"is missing {key!r}")
    return table[key]


def _string(value: object, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        _fail(path, "must be a non-empty string" if nonempty else "must be a string")
    if any(not character.isprintable() for character in value):
        _fail(path, "cannot contain control characters")
    return value


def _integer(
    value: object,
    path: str,
) -> int:
    if type(value) is not int:
        _fail(path, "must be an integer")
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _optional_string(
    table: Mapping[str, object], key: str, path: str
) -> str | None:
    return _string(table[key], f"{path}.{key}") if key in table else None


def _string_array(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(path, "must be an array of strings")
    result = []
    for index, item in enumerate(value, start=1):
        result.append(_string(item, f"{path}[{index}]"))
    return tuple(result)


def _parse_service_info(raw: object, name: str) -> ServiceInfo:
    path = f"{name}: [service]"
    table = _table(
        raw,
        path,
        {"name", "application", "role", "description", "documentation"},
    )
    service_name = _string(_required(table, "name", path), f"{path}.name")
    documentation = (
        _string(table["documentation"], f"{path}.documentation")
        if "documentation" in table
        else None
    )
    return ServiceInfo(
        name=service_name,
        application=_string(
            _required(table, "application", path), f"{path}.application"
        ),
        role=_string(_required(table, "role", path), f"{path}.role"),
        description=_string(
            _required(table, "description", path), f"{path}.description"
        ),
        documentation=documentation,
    )


def _parse_host(raw: object, name: str) -> HostIdentity:
    path = f"{name}: [host]"
    table = _table(
        raw,
        path,
        {"username", "uid", "subid-start", "display-name"},
    )
    username = _string(_required(table, "username", path), f"{path}.username")
    uid = _integer(_required(table, "uid", path), f"{path}.uid")
    display_name = _string(
        _required(table, "display-name", path), f"{path}.display-name"
    )
    return HostIdentity(
        username=username,
        uid=uid,
        subid_start=_integer(
            _required(table, "subid-start", path),
            f"{path}.subid-start",
        ),
        display_name=display_name,
    )


def _parse_endpoints(raw: object, name: str) -> tuple[Endpoint, ...]:
    path = f"{name}: [container].endpoints"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        table = _table(
            item,
            item_path,
            {"name", "port", "protocol", "host", "consumers"},
        )
        address = None
        host_port = None
        if "host" in table:
            host = _string(table["host"], f"{item_path}.host")
            if host.startswith("["):
                match = re.fullmatch(r"\[([^]]+)]:(\d+)", host)
                expected_version = 6
            else:
                match = re.fullmatch(r"([^:]+):(\d+)", host)
                expected_version = 4
            if match is None:
                _fail(
                    f"{item_path}.host",
                    "must be an IPv4:port or [IPv6]:port endpoint",
                )
            try:
                address = ipaddress.ip_address(match.group(1))
            except ValueError:
                _fail(f"{item_path}.host", "contains an invalid IP address")
            if address.version != expected_version:
                _fail(f"{item_path}.host", "must bracket IPv6 addresses")
            host_port = int(match.group(2))
        protocol_text = _string(
            table.get("protocol", Protocol.TCP.value),
            f"{item_path}.protocol",
        )
        if protocol_text not in {item.value for item in Protocol}:
            _fail(f"{item_path}.protocol", 'must be "tcp" or "udp"')
        protocol = Protocol(protocol_text)
        result.append(
            Endpoint(
                name=_string(
                    _required(table, "name", item_path), f"{item_path}.name"
                ),
                port=_integer(
                    _required(table, "port", item_path), f"{item_path}.port"
                ),
                protocol=protocol,
                host_address=address,
                host_port=host_port,
                consumers=_string_array(
                    table.get("consumers", []), f"{item_path}.consumers"
                ),
            )
        )
    return tuple(result)


def _parse_dns(raw: object, name: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    path = f"{name}: [container].dns"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of IP addresses")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        text = _string(item, item_path)
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            _fail(item_path, "must be a valid IP address")
        result.append(address)
    return tuple(result)


def _parse_sysctls(raw: object, name: str) -> tuple[str, ...]:
    path = f"{name}: [container].sysctls"
    if raw is None:
        return ()
    return _string_array(raw, path)


def _parse_environment(raw: object, name: str) -> tuple[tuple[str, str], ...]:
    path = f"{name}: [container.environment]"
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        _fail(path, "must be a table")
    result = []
    for key, value in raw.items():
        environment_name = _string(key, f"{path} key")
        environment_value = _string(
            value,
            f"{path}.{environment_name}",
            nonempty=False,
        )
        result.append((environment_name, environment_value))
    return tuple(result)


def _parse_volumes(raw: object, name: str) -> tuple[VolumeMount, ...]:
    path = f"{name}: [container].volumes"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        table = _table(item, item_path, {"source", "target", "options", "comment"})
        source = _string(
            _required(table, "source", item_path),
            f"{item_path}.source",
        )
        target = _string(
            _required(table, "target", item_path),
            f"{item_path}.target",
        )
        options = _optional_string(table, "options", item_path)
        result.append(
            VolumeMount(
                source=source,
                target=target,
                options=options,
                comment=_optional_string(table, "comment", item_path),
            )
        )
    return tuple(result)


def _parse_secrets(raw: object, name: str) -> tuple[SecretMount, ...]:
    path = f"{name}: [container].secrets"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        table = _table(item, item_path, {"name", "target"})
        secret_name = _string(_required(table, "name", item_path), f"{item_path}.name")
        target = (
            _string(table["target"], f"{item_path}.target")
            if "target" in table
            else None
        )
        result.append(SecretMount(secret_name, target))
    return tuple(result)


def _parse_container(raw: object, name: str) -> ContainerSpec:
    path = f"{name}: [container]"
    table = _table(
        raw,
        path,
        {
            "image", "log-driver", "entrypoint", "enabled", "network", "container-user",
            "health-cmd",
            "no-new-privileges", "drop-capabilities", "shm-size-mib", "dns",
            "sysctls", "environment", "volumes", "secrets", "endpoints", "exec",
        },
    )
    image = _string(_required(table, "image", path), f"{path}.image")
    log_driver = _optional_string(table, "log-driver", path)
    if log_driver is not None and log_driver != "journald":
        _fail(f"{path}.log-driver", 'currently supports only "journald"')
    entrypoint = _optional_string(table, "entrypoint", path)
    health_cmd_text = _optional_string(table, "health-cmd", path)
    if health_cmd_text is not None and health_cmd_text != "none":
        _fail(f"{path}.health-cmd", 'currently supports only "none"')
    health_cmd: Literal["none"] | None = (
        "none" if health_cmd_text is not None else None
    )
    network_text = _optional_string(table, "network", path)
    if network_text is not None and network_text != "host":
        _fail(f"{path}.network", 'currently supports only "host"')
    network: Literal["host"] | None = (
        "host" if network_text is not None else None
    )
    exec_text = _optional_string(table, "exec", path)
    return ContainerSpec(
        image=image,
        log_driver=log_driver,
        entrypoint=entrypoint,
        enabled=_boolean(table["enabled"], f"{path}.enabled") if "enabled" in table else True,
        network=network,
        container_user=_integer(table["container-user"], f"{path}.container-user")
        if "container-user" in table else None,
        health_cmd=health_cmd,
        no_new_privileges=_boolean(
            table.get("no-new-privileges", False),
            f"{path}.no-new-privileges",
        ),
        drop_capabilities=_string_array(
            table.get("drop-capabilities", []),
            f"{path}.drop-capabilities",
        ),
        shm_size_mib=(
            _integer(table["shm-size-mib"], f"{path}.shm-size-mib")
            if "shm-size-mib" in table
            else None
        ),
        dns=_parse_dns(table.get("dns"), name),
        sysctls=_parse_sysctls(table.get("sysctls"), name),
        environment=_parse_environment(table.get("environment"), name),
        volumes=_parse_volumes(table.get("volumes"), name),
        secrets=_parse_secrets(table.get("secrets"), name),
        endpoints=_parse_endpoints(table.get("endpoints"), name),
        exec=exec_text,
    )


def _parse_identity(raw: object, name: str) -> ServiceIdentity:
    path = f"{name}: [identity]"
    table = _table(
        raw,
        path,
        {"supplemental-groups", "mapped-container-id", "mapped-group"},
        required=False,
    )
    return ServiceIdentity(
        supplemental_groups=_string_array(
            table.get("supplemental-groups", []),
            f"{path}.supplemental-groups",
        ),
        mapped_container_id=(
            _integer(
                table["mapped-container-id"],
                f"{path}.mapped-container-id",
            )
            if "mapped-container-id" in table
            else None
        ),
        mapped_group=_optional_string(table, "mapped-group", path),
    )


def _parse_host_access(raw: object, name: str) -> tuple[int, ...]:
    path = f"{name}: [krun].host-access"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of TCP ports")
    ports = tuple(
        _integer(port, f"{path}[{index}]")
        for index, port in enumerate(raw, start=1)
    )
    return ports


def _parse_krun(raw: object, name: str, container: ContainerSpec) -> KrunSpec | None:
    if raw is None:
        return None
    path = f"{name}: [krun]"
    table = _table(
        raw,
        path,
        {
            "enabled",
            "cpus",
            "ram-mib",
            "network",
            "ipv4",
            "probe-endpoint",
            "probe-timeout-sec",
            "host-access",
            "egress",
        },
    )
    enabled = _boolean(_required(table, "enabled", path), f"{path}.enabled")
    if not enabled:
        extra = sorted(set(table) - {"enabled"})
        if extra:
            _fail(path, f"fields are not allowed when disabled: {', '.join(extra)}")
        return None
    cpus = _integer(_required(table, "cpus", path), f"{path}.cpus")
    ram_mib = _integer(_required(table, "ram-mib", path), f"{path}.ram-mib")
    network_text = _string(
        table.get("network", KrunNetwork.TSI.value),
        f"{path}.network",
    )
    if network_text not in {item.value for item in KrunNetwork}:
        _fail(f"{path}.network", 'must be "tsi", "passt", or "tap"')
    network = KrunNetwork(network_text)
    egress = table.get("egress")
    if egress is not None:
        egress = _string(egress, f"{path}.egress")
        if egress != "mullvad":
            _fail(f"{path}.egress", 'must be "mullvad"')
    tap_only_present = any(
        key in table
        for key in (
            "ipv4",
            "probe-endpoint",
            "probe-timeout-sec",
            "host-access",
        )
    )
    if network is KrunNetwork.TAP:
        try:
            ipv4_text = _string(_required(table, "ipv4", path), f"{path}.ipv4")
            parsed = ipaddress.ip_interface(ipv4_text)
        except (TypeError, ValueError):
            _fail(f"{path}.ipv4", "must be an IPv4 interface address")
        if not isinstance(parsed, ipaddress.IPv4Interface):
            _fail(f"{path}.ipv4", "must be an IPv4 interface address")
        ipv4 = parsed
        probe_endpoint = _string(
            _required(table, "probe-endpoint", path),
            f"{path}.probe-endpoint",
        )
        return KrunTap(
            cpus=cpus,
            ram_mib=ram_mib,
            ipv4=ipv4,
            probe_endpoint=probe_endpoint,
            probe_timeout_sec=_integer(
                table.get("probe-timeout-sec", 30),
                f"{path}.probe-timeout-sec",
            ),
            host_access=_parse_host_access(table.get("host-access"), name),
            egress=egress,
        )
    if tap_only_present:
        _fail(path, 'TAP-only fields require network = "tap"')
    if network is KrunNetwork.PASST:
        return KrunPasst(cpus, ram_mib, egress=egress)
    return KrunTsi(cpus, ram_mib, egress=egress)


def _parse_assets(
    raw: object,
    name: str,
) -> AssetsSpec | None:
    if raw is None:
        return None
    path = f"{name}: [assets]"
    table = _table(raw, path, {"path"})
    asset_path = _string(
        _required(table, "path", path),
        f"{path}.path",
    )
    return AssetsSpec(asset_path)


def _parse_unit(raw: object, name: str) -> UnitSpec:
    if raw is None:
        return UnitSpec()
    path = f"{name}: [unit]"
    table = _table(raw, path, {"restart-sec", "timeout-start-sec"})
    return UnitSpec(
        restart_sec=_integer(table.get("restart-sec", 30), f"{path}.restart-sec"),
        timeout_start_sec=_integer(table["timeout-start-sec"], f"{path}.timeout-start-sec")
        if "timeout-start-sec" in table else None,
    )


def _parse_dependencies(raw: object, name: str) -> tuple[Dependency, ...]:
    path = f"{name}: [[startup.dependencies]]"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        table = _table(
            item,
            item_path,
            {
                "service",
                "endpoint",
                "condition",
                "path",
                "timeout-sec",
                "interval-sec",
            },
        )
        condition_text = _string(
            _required(table, "condition", item_path),
            f"{item_path}.condition",
        )
        try:
            condition = DependencyCondition(condition_text)
        except ValueError:
            _fail(f"{item_path}.condition", 'must be "tcp" or "http"')
        result.append(
            Dependency(
                service=_string(
                    _required(table, "service", item_path),
                    f"{item_path}.service",
                ),
                endpoint=_string(
                    _required(table, "endpoint", item_path),
                    f"{item_path}.endpoint",
                ),
                condition=condition,
                timeout_sec=_integer(
                    _required(table, "timeout-sec", item_path),
                    f"{item_path}.timeout-sec",
                ),
                interval_sec=_integer(
                    _required(table, "interval-sec", item_path),
                    f"{item_path}.interval-sec",
                ),
                path=_optional_string(table, "path", item_path),
            )
        )
    return tuple(result)


def _parse_startup(
    raw: object,
    name: str,
) -> StartupSpec:
    if raw is None:
        return StartupSpec()
    path = f"{name}: [startup]"
    table = _table(
        raw,
        path,
        {"dependencies", "require-published-tcp-ports-free"},
    )
    require_free_ports = (
        _boolean(
            table["require-published-tcp-ports-free"],
            f"{path}.require-published-tcp-ports-free",
        )
        if "require-published-tcp-ports-free" in table
        else False
    )
    return StartupSpec(
        dependencies=_parse_dependencies(table.get("dependencies"), name),
        require_published_tcp_ports_free=require_free_ports,
    )


def _load_fleet_tables(toml_path: Path) -> dict[str, object]:
    with toml_path.open("rb") as config_file:
        raw = tomllib.load(config_file)
    name = toml_path.name
    return _table(
        raw,
        name,
        {
            "groups",
            "resources",
            "egress",
            "host-secret-consumers",
            "host-vm-taps",
        },
    )


def load_host_vm_taps(toml_path: Path) -> tuple[HostVmTap, ...]:
    name = toml_path.name
    top = _load_fleet_tables(toml_path)
    raw = top.get("host-vm-taps", [])
    path = f"{name}: [[host-vm-taps]]"
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        table = _table(
            item,
            item_path,
            {"name", "interface", "ipv4", "managed-units"},
        )
        ipv4_text = _string(
            _required(table, "ipv4", item_path),
            f"{item_path}.ipv4",
        )
        try:
            ipv4 = ipaddress.ip_interface(ipv4_text)
        except ValueError:
            _fail(f"{item_path}.ipv4", "must be an IPv4 interface address")
        if not isinstance(ipv4, ipaddress.IPv4Interface):
            _fail(f"{item_path}.ipv4", "must be an IPv4 interface address")
        result.append(
            HostVmTap(
                name=_string(
                    _required(table, "name", item_path),
                    f"{item_path}.name",
                ),
                interface=_string(
                    _required(table, "interface", item_path),
                    f"{item_path}.interface",
                ),
                ipv4=ipv4,
                managed_units=_string_array(
                    _required(table, "managed-units", item_path),
                    f"{item_path}.managed-units",
                ),
            )
        )
    return tuple(result)


def load_host_secret_consumers(
    toml_path: Path,
) -> tuple[HostSecretConsumer, ...]:
    name = toml_path.name
    top = _load_fleet_tables(toml_path)
    raw = top.get("host-secret-consumers", [])
    path = f"{name}: [[host-secret-consumers]]"
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        table = _table(item, item_path, {"name", "secrets"})
        result.append(
            HostSecretConsumer(
                name=_string(
                    _required(table, "name", item_path),
                    f"{item_path}.name",
                ),
                secrets=_string_array(
                    _required(table, "secrets", item_path),
                    f"{item_path}.secrets",
                ),
            )
        )
    return tuple(result)


def load_fleet_config(toml_path: Path) -> tuple[FleetGroup, ...]:
    name = toml_path.name
    top = _load_fleet_tables(toml_path)
    groups = top.get("groups", [])
    if not isinstance(groups, list):
        _fail(f"{name}: [[groups]]", "must be an array of tables")
    result = []
    for index, item in enumerate(groups, start=1):
        path = f"{name}: [[groups]][{index}]"
        table = _table(item, path, {"name", "gid"})
        result.append(
            FleetGroup(
                name=_string(
                    _required(table, "name", path),
                    f"{path}.name",
                ),
                gid=_integer(
                    _required(table, "gid", path),
                    f"{path}.gid",
                ),
            )
        )
        if result[-1].gid < 1:
            _fail(f"{path}.gid", "must be at least 1")
    return tuple(result)


def load_fleet_storage(toml_path: Path):
    name = toml_path.name
    top = _load_fleet_tables(toml_path)
    return parse_fleet_storage(top.get("resources"), name)


def _parse_mullvad_endpoint(
    raw: object,
    path: str,
) -> tuple[ipaddress.IPv4Address, int]:
    text = _string(raw, path)
    match = re.fullmatch(r"([^:]+):(\d+)", text)
    if match is None:
        _fail(path, "must be an IPv4 address and port")
    try:
        address = ipaddress.ip_address(match.group(1))
    except ValueError:
        _fail(path, "contains an invalid IP address")
    if not isinstance(address, ipaddress.IPv4Address):
        _fail(path, "must use an IPv4 address")
    return address, int(match.group(2))


def _parse_mullvad_egress(raw: object, name: str) -> MullvadEgress | None:
    if raw is None:
        return None
    path = f"{name}: [egress.mullvad]"
    table = _table(
        raw,
        path,
        {
            "interface",
            "address",
            "peer-public-key",
            "endpoint",
            "allowed-ips",
            "secret",
            "route-table",
            "firewall-mark",
        },
    )
    address_text = _string(
        _required(table, "address", path),
        f"{path}.address",
    )
    try:
        address = ipaddress.ip_interface(address_text)
    except ValueError:
        _fail(f"{path}.address", "must be an IPv4 interface address")
    if not isinstance(address, ipaddress.IPv4Interface):
        _fail(f"{path}.address", "must be an IPv4 interface address")
    endpoint_address, endpoint_port = _parse_mullvad_endpoint(
        _required(table, "endpoint", path),
        f"{path}.endpoint",
    )
    allowed_raw = _required(table, "allowed-ips", path)
    if not isinstance(allowed_raw, list):
        _fail(f"{path}.allowed-ips", "must be an array of IPv4 networks")
    allowed_ips = []
    for index, item in enumerate(allowed_raw, start=1):
        item_path = f"{path}.allowed-ips[{index}]"
        text = _string(item, item_path)
        try:
            network = ipaddress.ip_network(text, strict=True)
        except ValueError:
            _fail(item_path, "must be an IPv4 network")
        if not isinstance(network, ipaddress.IPv4Network):
            _fail(item_path, "must be an IPv4 network")
        allowed_ips.append(network)
    return MullvadEgress(
        interface=_string(
            _required(table, "interface", path),
            f"{path}.interface",
        ),
        address=address,
        peer_public_key=_string(
            _required(table, "peer-public-key", path),
            f"{path}.peer-public-key",
        ),
        endpoint_address=endpoint_address,
        endpoint_port=endpoint_port,
        allowed_ips=tuple(allowed_ips),
        secret_name=_string(
            _required(table, "secret", path),
            f"{path}.secret",
        ),
        route_table=_integer(
            _required(table, "route-table", path),
            f"{path}.route-table",
        ),
        firewall_mark=_integer(
            _required(table, "firewall-mark", path),
            f"{path}.firewall-mark",
        ),
    )


def load_fleet_egress(toml_path: Path) -> MullvadEgress | None:
    top = _load_fleet_tables(toml_path)
    name = toml_path.name
    raw = top.get("egress")
    if raw is None:
        return None
    table = _table(raw, f"{name}: [egress]", {"mullvad"})
    return _parse_mullvad_egress(
        _required(table, "mullvad", f"{name}: [egress]"),
        name,
    )


def load_service(toml_path: Path) -> Service:
    with toml_path.open("rb") as config_file:
        raw = tomllib.load(config_file)
    name = toml_path.name
    top = _table(
        raw,
        name,
        {
            "service",
            "host",
            "container",
            "identity",
            "krun",
            "storage",
            "shared-storage",
            "assets",
            "startup",
            "unit",
        },
    )
    for required in ("service", "host", "container"):
        if required not in top:
            _fail(name, f"missing required [{required}] section")
    info = _parse_service_info(top["service"], name)
    host = _parse_host(top["host"], name)
    container = _parse_container(top["container"], name)
    krun = _parse_krun(top.get("krun"), name, container)
    return Service(
        source=toml_path,
        info=info,
        host=host,
        container=container,
        identity=_parse_identity(top.get("identity"), name),
        krun=krun,
        storage=parse_storage(top.get("storage"), name),
        shared_storage=parse_shared_storage(top.get("shared-storage"), name),
        assets=_parse_assets(top.get("assets"), name),
        startup=_parse_startup(top.get("startup"), name),
        unit=_parse_unit(top.get("unit"), name),
    )
