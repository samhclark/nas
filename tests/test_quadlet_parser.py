# ABOUTME: Tests strict TOML decoding into the rootless-service model.

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quadletgen.compiler import compile_fleet
from quadletgen.model import ConfigError, Fleet, Protocol
from quadletgen.parser import load_service
from quadletgen.storage_model import ManagedZfsStorage, StorageAccess
from tests.quadlet_test_support import REPO, service_toml


class StrictParserTests(unittest.TestCase):
    def load(self, source: str, filename: str = "service.toml"):
        with tempfile.TemporaryDirectory(dir=REPO) as directory:
            path = Path(directory) / filename
            path.write_text(source)
            return load_service(path)

    def assert_invalid(self, source: str, message: str) -> None:
        with self.assertRaisesRegex(ConfigError, message):
            self.load(source)

    def assert_invalid_field(self, source: str, field: str) -> None:
        with self.assertRaises(ConfigError) as raised:
            self.load(source)
        self.assertIn(field, str(raised.exception))

    def test_rejects_unknown_keys_at_every_closed_boundary(self):
        self.assert_invalid(
            service_toml(extra="\n[typo]\nvalue = true"),
            "unknown keys: typo",
        )
        self.assert_invalid(
            service_toml(container="imag = \"typo\""),
            "unknown keys: imag",
        )
        self.assert_invalid(
            service_toml(
                krun=(
                    "enabled = true\ncpus = 1\nram-mib = 128\n"
                    "network-mode = \"tap\""
                )
            ),
            "unknown keys: network-mode",
        )

    def test_rejects_superseded_network_and_readiness_shapes(self):
        endpoint_source = service_toml(
            container='''[[container.endpoints]]
name = "http"
port = 8080'''
        )
        self.assert_invalid(
            endpoint_source.replace("endpoints", "ports", 1),
            "unknown keys: ports",
        )
        self.assert_invalid(
            service_toml(
                extra='''
[startup.readiness]
url = "http://127.0.0.1:8080/health"'''
            ),
            "unknown keys: readiness",
        )
        self.assert_invalid(
            service_toml(
                container='''network = "host"
[[container.endpoints]]
name = "http"
port = 8080''',
                krun='''enabled = true
cpus = 1
ram-mib = 128
network = "tap"
ipv4 = "10.253.99.2/30"
probe-endpoint = "http"
[[krun.ingress]]
from = "peer"
ports = [8080]''',
            ),
            "unknown keys: ingress",
        )

    def test_application_and_role_are_required_metadata(self):
        service = self.load(
            service_toml(application="immich", role="server")
        )
        self.assertEqual(service.info.application, "immich")
        self.assertEqual(service.info.role, "server")
        self.assert_invalid(
            service_toml().replace('application = "service"\n', ""),
            "missing 'application'",
        )

    def test_requires_immutable_image_reference(self):
        self.assert_invalid(
            service_toml().replace(
                f"example.invalid/service:1@sha256:{'a' * 64}",
                "example.invalid/service:1",
            ),
            "immutable name:tag@sha256 digest",
        )

    def test_top_level_parser_integrates_typed_storage(self):
        service = self.load(
            service_toml(
                extra="""
[[storage]]
name = "state"
kind = "managed-zfs"
dataset = "tank/service/state"
host-path = "/var/lib/service"
mode = "0750"
record-size = "128K"
compression = "lz4"
atime = false
primary-cache = "all"

[[storage.exports]]
subpath = "."
container-path = "/state"
access = "read-write"
"""
            )
        )

        self.assertEqual(len(service.storage), 1)
        storage = service.storage[0]
        self.assertIsInstance(storage, ManagedZfsStorage)
        self.assertEqual(storage.exports[0].access, StorageAccess.READ_WRITE)

    def test_storage_policy_is_bound_to_the_service(self):
        managed = """
[[storage]]
name = "state"
kind = "managed-zfs"
dataset = "tank/other/state"
host-path = "/var/lib/service"
mode = "0750"
record-size = "128K"
compression = "lz4"
atime = false
primary-cache = "all"

[[storage.exports]]
subpath = "."
container-path = "/state"
access = "read-write"
"""
        self.assert_invalid(
            service_toml(extra=managed),
            "managed datasets must be below tank/service/",
        )
        self.assert_invalid(
            service_toml(
                container='''
[[container.volumes]]
source = "/var/lib/legacy"
target = "/state"
''',
                extra=managed.replace("tank/other/state", "tank/service/state"),
            ),
            "also a raw volume",
        )

    def test_service_identity_matches_dns_and_source_filename_contracts(self):
        for invalid_name in ("service-", "s" * 64):
            with self.subTest(name=invalid_name):
                self.assert_invalid(
                    service_toml().replace(
                        'name = "service"',
                        f'name = "{invalid_name}"',
                    ),
                    "must match",
                )

        with self.assertRaisesRegex(ConfigError, "filename must be"):
            self.load(service_toml(), filename="different.toml")

    def test_host_identity_stays_inside_repository_allocators(self):
        too_long_username = "_nas_" + "s" * 27
        self.assert_invalid_field(
            service_toml().replace(
                'username = "_nas_service"',
                f'username = "{too_long_username}"',
            ),
            "[host].username",
        )
        self.assert_invalid(
            service_toml(uid=50999),
            "must be at least 51000",
        )
        self.assert_invalid(
            service_toml(subid_start=2**32),
            "must be at most",
        )
        self.assert_invalid_field(
            service_toml().replace(
                'display-name = "Test service"',
                'display-name = "Test:service"',
            ),
            "[host].display-name",
        )

    def test_parses_typed_endpoints_dns_sysctls_and_secrets_in_source_order(self):
        service = self.load(
            service_toml(
                container="""
dns = ["100.100.100.100", "2001:db8::53"]
sysctls = ["net.ipv4.ip_forward=1"]

[[container.endpoints]]
name = "http"
host = "127.0.0.1:8080"
port = 80

[[container.endpoints]]
name = "dns"
host = "[0:0:0:0:0:0:0:1]:5353"
port = 5353
protocol = "udp"

[[container.secrets]]
name = "token"
"""
            )
        )

        self.assertEqual(
            [endpoint.protocol for endpoint in service.container.endpoints],
            [Protocol.TCP, Protocol.UDP],
        )
        self.assertEqual(
            [str(server) for server in service.container.dns],
            ["100.100.100.100", "2001:db8::53"],
        )
        self.assertEqual(
            service.container.endpoints[1].publication,
            "[::1]:5353",
        )
        self.assertEqual(service.container.sysctls, ("net.ipv4.ip_forward=1",))
        self.assertEqual(service.container.secrets[0].name, "token")

    def test_rejects_invalid_port_declarations(self):
        cases = {
            "hostname": 'host = "localhost:8080"\nport = 80',
            "unbracketed IPv6": 'host = "::1:8080"\nport = 80',
            "port zero": 'host = "127.0.0.1:0"\nport = 80',
            "boolean": 'host = "127.0.0.1:8080"\nport = true',
            "protocol": (
                'host = "127.0.0.1:8080"\nport = 80\n'
                'protocol = "sctp"'
            ),
        }
        for label, declaration in cases.items():
            with self.subTest(label=label):
                self.assert_invalid(
                    service_toml(
                        container=(
                            "\n[[container.endpoints]]\n"
                            f'name = "http"\n{declaration}'
                        )
                    ),
                    "container",
                )

    def test_enum_type_errors_remain_configuration_errors(self):
        self.assert_invalid_field(
            service_toml(
                container=(
                    "[[container.endpoints]]\n"
                    'name = "http"\n'
                    'host = "127.0.0.1:8080"\n'
                    "port = 80\n"
                    'protocol = ["tcp"]'
                )
            ),
            "[container].endpoints[1].protocol",
        )
        self.assert_invalid_field(
            service_toml(
                krun=(
                    "enabled = true\ncpus = 1\nram-mib = 128\n"
                    'network = ["tap"]'
                )
            ),
            "[krun].network",
        )

    def test_duplicate_ports_use_typed_canonical_endpoints(self):
        self.assert_invalid(
            service_toml(
                container=(
                    "[[container.endpoints]]\n"
                    'name = "dns-one"\n'
                    'host = "[::1]:5353"\n'
                    "port = 5353\n"
                    'protocol = "udp"\n\n'
                    "[[container.endpoints]]\n"
                    'name = "dns-two"\n'
                    'host = "[0:0:0:0:0:0:0:1]:5353"\n'
                    "port = 5354\n"
                    'protocol = "udp"'
                )
            ),
            "duplicate publication",
        )

    def test_rejects_host_ports_without_tap_exception(self):
        self.assert_invalid(
            service_toml(
                container=(
                    'network = "host"\n\n[[container.endpoints]]\n'
                    'name = "http"\n'
                    'host = "127.0.0.1:8080"\nport = 80'
                )
            ),
            "cannot be used with network",
        )

    def test_rejects_invalid_krun_variants(self):
        cases = {
            "missing enabled": "cpus = 1\nram-mib = 128",
            "disabled fields": "enabled = false\ncpus = 1",
            "low RAM": "enabled = true\ncpus = 1\nram-mib = 127",
            "passt host": (
                "enabled = true\ncpus = 1\nram-mib = 128\n"
                'network = "passt"'
            ),
        }
        for label, krun in cases.items():
            with self.subTest(label=label):
                container = 'network = "host"' if label == "passt host" else ""
                self.assert_invalid(
                    service_toml(container=container, krun=krun),
                    "krun",
                )

    def test_disabled_krun_normalizes_to_absence(self):
        self.assertIsNone(self.load(service_toml(krun="enabled = false")).krun)

    def test_tap_uses_named_probe_endpoint_and_derives_network_values(self):
        service = self.load(
            service_toml(
                container=(
                    'network = "host"\n\n[[container.endpoints]]\n'
                    'name = "admin"\n'
                    'host = "127.0.0.1:8081"\nport = 8081\n\n'
                    '[[container.endpoints]]\n'
                    'name = "http"\n'
                    'host = "127.0.0.1:8080"\nport = 8080'
                ),
                krun=(
                    "enabled = true\ncpus = 1\nram-mib = 128\n"
                    'network = "tap"\nipv4 = "10.253.99.2/30"\n'
                    "probe-timeout-sec = 120\n"
                    'probe-endpoint = "http"'
                ),
            )
        )

        self.assertTrue(service.active_tap)
        self.assertEqual(service.tap_name, "krun-51999")
        self.assertEqual(str(service.tap_guest), "10.253.99.2/30")
        self.assertEqual(str(service.tap_gateway), "10.253.99.1/30")
        self.assertEqual(service.tap_spec.probe_endpoint, "http")
        self.assertEqual(service.tap_spec.probe_timeout_sec, 120)
        self.assertFalse(service.tap_spec.guest_bootstrap_root)
        artifacts = {
            artifact.path: artifact.content
            for artifact in compile_fleet(Fleet.build([service]))
        }
        unit = artifacts[
            Path("etc/containers/systemd/users/51999/service.container")
        ]
        self.assertIn("/dev/tcp/10.253.99.2/8080", unit)
        self.assertNotIn("/dev/tcp/10.253.99.2/8081", unit)
        self.assertIn("for i in {1..120}", unit)
        self.assertNotIn("krun.guest_bootstrap_root", unit)

    def test_guest_root_bootstrap_retains_the_host_mapped_identity(self):
        source = service_toml(
            container='''network = "host"
container-user = 1000
entrypoint = "/usr/share/nas/service/entrypoint.sh"
[[container.endpoints]]
name = "http"
port = 8080''',
            krun='''enabled = true
cpus = 1
ram-mib = 128
network = "tap"
ipv4 = "10.253.99.2/30"
probe-endpoint = "http"
guest-bootstrap-root = true''',
        )
        service = self.load(source)
        self.assertTrue(service.tap_spec.guest_bootstrap_root)
        unit = next(
            artifact.content
            for artifact in compile_fleet(Fleet.build([service]))
            if artifact.path.name == "service.container"
        )
        self.assertIn("Annotation=krun.guest_bootstrap_root=1", unit)
        self.assertIn("User=1000:1000\n", unit)
        self.assertIn("UserNS=keep-id:uid=1000,gid=1000", unit)
        for replacement in (
            'container-user = 0',
            '',
        ):
            self.assert_invalid(
                source.replace('container-user = 1000', replacement),
                "requires a positive container-user",
            )
        self.assert_invalid(
            source.replace('entrypoint = "/usr/share/nas/service/entrypoint.sh"', ''),
            "explicit entrypoint",
        )
        self.assert_invalid_field(
            source.replace('guest-bootstrap-root = true', 'guest-bootstrap-root = 1'),
            "[krun].guest-bootstrap-root",
        )
        self.assert_invalid(
            service_toml(
                krun='''enabled = true
cpus = 1
ram-mib = 128
guest-bootstrap-root = true''',
            ),
            "TAP-only fields",
        )

    def test_tap_probe_must_reference_a_declared_tcp_endpoint(self):
        container = (
            'network = "host"\n\n[[container.endpoints]]\n'
            'name = "dns"\n'
            'host = "127.0.0.1:8080"\nport = 8080\n'
            'protocol = "udp"'
        )
        base_krun = (
            "enabled = true\ncpus = 1\nram-mib = 128\n"
            'network = "tap"\nipv4 = "10.253.99.2/30"'
        )

        self.assert_invalid(
            service_toml(container=container, krun=base_krun),
            "missing 'probe-endpoint'",
        )
        self.assert_invalid(
            service_toml(
                container=container,
                krun=f'{base_krun}\nprobe-endpoint = "dns"',
            ),
            "declared TCP endpoint",
        )
        self.assert_invalid(
            service_toml(
                container=(
                    f"{container}\n\n"
                    "[[container.endpoints]]\n"
                    'name = "health"\n'
                    'host = "127.0.0.1:9090"\n'
                    "port = 9090"
                ),
                krun=(
                    f'{base_krun}\nprobe-endpoint = "missing"'
                ),
            ),
            "declared TCP endpoint",
        )

    def test_health_cmd_supports_only_explicit_disable(self):
        service = self.load(service_toml(container='health-cmd = "none"'))
        self.assertEqual(service.container.health_cmd, "none")
        self.assert_invalid(
            service_toml(container='health-cmd = "curl /health"'),
            "supports only",
        )

    def test_log_driver_supports_only_journald(self):
        service = self.load(service_toml(container='log-driver = "journald"'))
        self.assertEqual(service.container.log_driver, "journald")
        self.assert_invalid(
            service_toml(container='log-driver = "json-file"'),
            "supports only",
        )

    def test_container_hardening_and_shared_memory_are_typed(self):
        service = self.load(
            service_toml(
                container='''no-new-privileges = true
drop-capabilities = ["NET_RAW", "SYS_CHROOT"]
shm-size-mib = 128'''
            )
        )
        self.assertTrue(service.container.no_new_privileges)
        self.assertEqual(
            service.container.drop_capabilities,
            ("NET_RAW", "SYS_CHROOT"),
        )
        self.assertEqual(service.container.shm_size_mib, 128)

        self.assert_invalid(
            service_toml(container='drop-capabilities = ["net_raw"]'),
            "invalid Linux capability",
        )
        self.assert_invalid(
            service_toml(container="shm-size-mib = 0"),
            "must be at least 1",
        )

    def test_single_line_fields_reject_directive_injection(self):
        cases = {
            "[service].description": service_toml().replace(
                'description = "Test service"',
                'description = "Test service\\nRequires=evil.service"',
            ),
            "[service].documentation": service_toml().replace(
                'description = "Test service"',
                'description = "Test service"\n'
                'documentation = "https://example.invalid/\\nRequires=evil.service"',
            ),
            "[host].display-name": service_toml().replace(
                'display-name = "Test service"',
                'display-name = "Test service\\nZ /tmp 0777 root root -"',
            ),
            "[container].exec": service_toml(
                container='exec = "server\\nEnvironment=EVIL=1"'
            ),
            "[container].volumes[1].comment": service_toml(
                container=(
                    "[[container.volumes]]\n"
                    'source = "/var/lib/service"\n'
                    'target = "/data"\n'
                    'comment = "safe\\nVolume=/tmp:/escape"'
                )
            ),
        }
        for field, source in cases.items():
            with self.subTest(field=field):
                self.assert_invalid_field(source, field)

    def test_unit_atoms_reject_ambiguous_systemd_syntax(self):
        cases = {
            "[container].image": service_toml().replace(
                "example.invalid/service:1@sha256:",
                "example.invalid/%service:1@sha256:",
            ),
            "[container.environment].BAD": service_toml(
                container='[container.environment]\nBAD = "two words"'
            ),
            "[container.environment].DOLLAR": service_toml(
                container=(
                    "[container.environment]\n"
                    'DOLLAR = "${USER}"'
                )
            ),
            "[container].sysctls": service_toml(
                container='sysctls = ["net.ipv4.ip_forward=%n"]'
            ),
            "[container].exec": service_toml(container='exec = "server; reboot"'),
            "[container].entrypoint": service_toml(
                container='entrypoint = "server; reboot"'
            ),
        }
        for field, source in cases.items():
            with self.subTest(field=field):
                self.assert_invalid_field(source, field)

    def test_environment_names_are_identifiers(self):
        for environment_name in ("BAD-NAME", "1BAD"):
            with self.subTest(name=environment_name):
                self.assert_invalid_field(
                    service_toml(
                        container=(
                            "[container.environment]\n"
                            f'"{environment_name}" = "value"'
                        )
                    ),
                    "[container.environment] key",
                )

    def test_paths_and_modes_match_their_output_grammars(self):
        cases = {
            "[container].volumes[1].source": service_toml(
                container=(
                    "[[container.volumes]]\n"
                    'source = "/var/lib/../escape"\n'
                    'target = "/data"'
                )
            ),
            "[container].volumes[1].target": service_toml(
                container=(
                    "[[container.volumes]]\n"
                    'source = "/var/lib/service"\n'
                    'target = "/data:other"'
                )
            ),
            "[container].volumes[1].options": service_toml(
                container=(
                    "[[container.volumes]]\n"
                    'source = "/usr/share/service"\n'
                    'target = "/data"\n'
                    'options = "ro;rw"'
                )
            ),
            "[container].secrets[1].target": service_toml(
                container=(
                    "[[container.secrets]]\n"
                    'name = "token"\n'
                    'target = "relative/path"'
                )
            ),
            "[assets].path": service_toml(
                extra=(
                    "[assets]\n"
                    'path = "/usr/share/nas/somewhere-else"'
                )
            ),
        }
        for field, source in cases.items():
            with self.subTest(field=field):
                self.assert_invalid_field(source, field)

    def test_manifest_fields_reject_delimiters(self):
        self.assert_invalid(
            service_toml(
                container=(
                    "[[container.secrets]]\n"
                    'name = "bad\\tname"'
                )
            ),
            "control characters",
        )
        self.assert_invalid(
            service_toml(
                extra=(
                    "[assets]\n"
                    'path = "/usr/share/bad\\npath"'
                )
            ),
            "control characters",
        )

    def test_startup_dependencies_are_typed_and_repeatable(self):
        service = self.load(
            service_toml(
                extra="""
[[startup.dependencies]]
service = "immich-database"
endpoint = "postgres"
condition = "tcp"
timeout-sec = 300
interval-sec = 2

[[startup.dependencies]]
service = "immich-machine-learning"
endpoint = "http"
condition = "http"
path = "/ping"
timeout-sec = 600
interval-sec = 5
"""
            )
        )

        self.assertEqual(len(service.startup.dependencies), 2)
        self.assertEqual(service.startup.dependencies[0].service, "immich-database")
        self.assertEqual(service.startup.dependencies[1].path, "/ping")

    def test_startup_dependency_contract_is_closed(self):
        cases = {
            "bad condition": 'condition = "udp"',
            "HTTP without path": 'condition = "http"',
            "TCP with path": 'condition = "tcp"\npath = "/ready"',
            "interval exceeds timeout": (
                'condition = "tcp"\ntimeout-sec = 1\ninterval-sec = 2'
            ),
        }
        base = (
            "[[startup.dependencies]]\n"
            'service = "database"\n'
            'endpoint = "postgres"\n'
        )
        for label, fields in cases.items():
            with self.subTest(label=label):
                timing = (
                    "\ntimeout-sec = 5\ninterval-sec = 1"
                    if "timeout-sec" not in fields
                    else ""
                )
                self.assert_invalid(
                    service_toml(extra=base + fields + timing),
                    "startup.*dependencies",
                )

    def test_startup_port_guard_is_validated(self):
        self.assert_invalid(
            service_toml(
                extra=(
                    "[startup]\n"
                    "require-published-tcp-ports-free = true"
                )
            ),
            "requires at least one published TCP port",
        )

    def test_mutable_var_mounts_require_typed_storage(self):
        self.assert_invalid(
            service_toml(
                container=(
                    "[[container.volumes]]\n"
                    'source = "/var/lib/service"\n'
                    'target = "/data"'
                )
            ),
            r"must use \[\[storage\]\]",
        )
        self.assert_invalid(
            service_toml(extra='[data]\npath = "/var/lib/service"'),
            "unknown keys: data",
        )


if __name__ == "__main__":
    unittest.main()
