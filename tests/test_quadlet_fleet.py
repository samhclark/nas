# ABOUTME: Tests cross-service invariants for the compiled rootless fleet.

from __future__ import annotations

import tempfile
import unittest
import ipaddress
from dataclasses import replace
from pathlib import Path

from quadletgen.compiler import compile_fleet
from quadletgen.model import (
    ConfigError,
    Fleet,
    HostVmTap,
)
from quadletgen.parser import load_host_vm_taps, load_service
from tests.quadlet_test_support import REPO, current_fleet, service_toml


class FleetValidationTests(unittest.TestCase):
    def write(self, directory: Path, filename: str, source: str):
        path = directory / filename
        path.write_text(source)
        return load_service(path)

    def test_direct_model_construction_cannot_bypass_local_invariants(self):
        service = next(
            item
            for item in current_fleet().services
            if not item.storage and item.container.endpoints
        )
        endpoint = service.container.endpoints[0]
        invalid_changes = {
            "service name": lambda: replace(
                service,
                info=replace(service.info, name="../../../../outside"),
            ),
            "container image": lambda: replace(
                service,
                container=replace(
                    service.container,
                    image="example.invalid/service:latest",
                ),
            ),
            "host UID": lambda: replace(
                service,
                host=replace(service.host, uid=1),
            ),
            "endpoint name": lambda: replace(
                service,
                container=replace(
                    service.container,
                    endpoints=(
                        replace(endpoint, name="../../outside"),
                        *service.container.endpoints[1:],
                    ),
                ),
            ),
        }
        for label, make_invalid in invalid_changes.items():
            with self.subTest(label=label):
                with self.assertRaises(ConfigError):
                    make_invalid()

    def test_rejects_duplicate_identity_and_overlapping_subids(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            first = self.write(
                directory,
                "first.toml",
                service_toml(name="first", uid=51991, subid_start=600000000),
            )
            duplicate_uid = self.write(
                directory,
                "second.toml",
                service_toml(name="second", uid=51991, subid_start=700000000),
            )
            with self.assertRaisesRegex(ConfigError, "duplicate uid"):
                Fleet.build([first, duplicate_uid])

            overlapping = self.write(
                directory,
                "third.toml",
                service_toml(name="third", uid=51992, subid_start=600000001),
            )
            with self.assertRaisesRegex(ConfigError, "ranges overlap"):
                Fleet.build([first, overlapping])

    def test_host_vm_tap_is_typed_and_cannot_overlap_service_network(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            fleet_path = directory / "_fleet.toml"
            fleet_path.write_text(
                '''[[host-vm-taps]]
name = "immich-backup"
interface = "krun-backup"
ipv4 = "10.253.19.2/30"
managed-units = ["nas-backup-immich.service"]
'''
            )
            taps = load_host_vm_taps(fleet_path)
            self.assertEqual(taps[0].tap_name, "krun-backup")
            self.assertEqual(str(taps[0].tap_gateway), "10.253.19.1/30")

            service = self.write(
                directory,
                "service.toml",
                service_toml(
                    container=(
                        'network = "host"\n\n'
                        "[[container.endpoints]]\n"
                        'name = "http"\nport = 8080'
                    ),
                    krun=(
                        "enabled = true\ncpus = 1\nram-mib = 128\n"
                        'network = "tap"\n'
                        'ipv4 = "10.253.19.2/30"\n'
                        'probe-endpoint = "http"'
                    ),
                ),
            )
            with self.assertRaisesRegex(ConfigError, "host VM TAP subnet"):
                Fleet.build([service], host_vm_taps=taps)

    def test_host_vm_tap_rejects_invalid_interface_and_guest_address(self):
        invalid_interface = HostVmTap(
            name="backup",
            interface="tap-backup",
            ipv4=ipaddress.ip_interface("10.253.19.2/30"),
            managed_units=("nas-backup.service",),
        )
        with self.assertRaisesRegex(ConfigError, "beginning with 'krun-'"):
            replace(current_fleet(), host_vm_taps=(invalid_interface,))

        invalid_address = replace(
            invalid_interface,
            interface="krun-backup",
            ipv4=ipaddress.ip_interface("10.253.19.1/30"),
        )
        with self.assertRaisesRegex(ConfigError, "second guest address"):
            replace(current_fleet(), host_vm_taps=(invalid_address,))

    def test_application_roles_are_unique_within_an_application(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            first = self.write(
                directory,
                "first.toml",
                service_toml(
                    name="first",
                    uid=51991,
                    subid_start=600000000,
                    application="immich",
                    role="server",
                ),
            )
            second = self.write(
                directory,
                "second.toml",
                service_toml(
                    name="second",
                    uid=51992,
                    subid_start=700000000,
                    application="immich",
                    role="server",
                ),
            )
            with self.assertRaisesRegex(ConfigError, "duplicate role 'server'"):
                Fleet.build([first, second])

    def test_rejects_host_port_collisions_for_non_tap_publishers(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            first = self.write(
                directory,
                "first.toml",
                service_toml(
                    name="first",
                    uid=51970,
                    subid_start=600000000,
                    container=(
                        "[[container.endpoints]]\n"
                        'name = "http"\n'
                        'host = "127.0.0.1:8080"\n'
                        "port = 8080"
                    ),
                ),
            )
            second = self.write(
                directory,
                "second.toml",
                service_toml(
                    name="second",
                    uid=51980,
                    subid_start=700000000,
                    container=(
                        "[[container.endpoints]]\n"
                        'name = "http"\n'
                        'host = "127.0.0.1:8080"\n'
                        "port = 8080"
                    ),
                ),
            )
            tap = self.write(
                directory,
                "tap.toml",
                service_toml(
                    name="tap",
                    uid=51990,
                    subid_start=800000000,
                    container=(
                        'network = "host"\n\n'
                        "[[container.endpoints]]\n"
                        'name = "http"\n'
                        'host = "127.0.0.1:9090"\n'
                        "port = 9090"
                    ),
                    krun=(
                        "enabled = true\ncpus = 1\nram-mib = 128\n"
                        'network = "tap"\n'
                        'ipv4 = "10.253.99.2/30"\n'
                        'probe-endpoint = "http"'
                    ),
                ),
            )

            with self.assertRaisesRegex(
                ConfigError,
                "host tcp port 8080 is also published by first.toml",
            ):
                Fleet.build([first, second, tap])

    def test_disabled_service_keeps_identity_but_has_no_runtime_artifacts(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            disabled = self.write(
                directory,
                "disabled.toml",
                service_toml(
                    name="disabled",
                    container=(
                        "enabled = false\n"
                        'network = "host"\n\n'
                        "[[container.endpoints]]\n"
                        'name = "http"\n'
                        'host = "127.0.0.1:8080"\n'
                        "port = 8080\n\n"
                        "[[container.secrets]]\n"
                        'name = "disabled-token"'
                    ),
                    krun=(
                        "enabled = true\ncpus = 1\nram-mib = 128\n"
                        'network = "tap"\n'
                        'ipv4 = "10.253.99.2/30"\n'
                        'probe-endpoint = "http"'
                    ),
                    extra=(
                        "\n[assets]\n"
                        'path = "/usr/share/nas/disabled"'
                    ),
                ),
            )
            active = self.write(
                directory,
                "active.toml",
                service_toml(
                    name="active",
                    uid=51990,
                    subid_start=800000000,
                    container=(
                        'network = "host"\n\n'
                        "[[container.endpoints]]\n"
                        'name = "http"\n'
                        'host = "127.0.0.1:8080"\n'
                        "port = 8080"
                    ),
                    krun=(
                        "enabled = true\ncpus = 1\nram-mib = 128\n"
                        'network = "tap"\n'
                        'ipv4 = "10.253.98.2/30"\n'
                        'probe-endpoint = "http"'
                    ),
                ),
            )
            fleet = Fleet.build([disabled, active])
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(fleet)
            }
            paths = set(artifacts)

        self.assertIn(Path("usr/lib/sysusers.d/nas-disabled.conf"), paths)
        self.assertIn(Path("etc/subuid"), paths)
        self.assertNotIn(
            Path("etc/containers/systemd/users/51999/disabled.container"),
            paths,
        )
        self.assertEqual(
            [service.info.name for service in fleet.active_taps],
            ["active"],
        )
        self.assertIn(
            "ensure-nas-disabled-account.service",
            artifacts[Path("usr/share/nas/fleet/account-units.list")],
        )
        self.assertNotIn(
            "krun-51999\t",
            artifacts[Path("usr/share/nas/fleet/active-taps.tsv")],
        )
        self.assertIn(
            "disabled\t_nas_disabled\tdisabled-token",
            artifacts[Path("usr/share/nas/fleet/secrets.tsv")],
        )
        self.assertIn(
            "/usr/share/nas/disabled",
            artifacts[Path("usr/share/nas/fleet/assets.list")],
        )

    def test_fleet_requires_at_least_one_active_tap(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            service = self.write(
                directory,
                "service.toml",
                service_toml(),
            )
            with self.assertRaisesRegex(
                ConfigError,
                "must contain at least one active TAP service",
            ):
                Fleet.build([service])

    def test_active_fleet_without_assets_emits_a_header_only_manifest(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            service = self.write(
                directory,
                "service.toml",
                service_toml(
                    container=(
                        'network = "host"\n\n'
                        "[[container.endpoints]]\n"
                        'name = "http"\n'
                        'host = "127.0.0.1:8080"\n'
                        "port = 8080"
                    ),
                    krun=(
                        "enabled = true\ncpus = 1\nram-mib = 128\n"
                        'network = "tap"\n'
                        'ipv4 = "10.253.99.2/30"\n'
                        'probe-endpoint = "http"'
                    ),
                ),
            )
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(Fleet.build([service]))
            }

        assets = artifacts[
            Path("usr/share/nas/fleet/assets.list")
        ]
        self.assertEqual(
            [line for line in assets.splitlines() if not line.startswith("#")],
            [],
        )
        account_script = artifacts[
            Path("usr/local/bin/ensure-nas-service-account.sh")
        ]
        self.assertNotIn("semanage fcontext", account_script)
        self.assertNotIn("restorecon", account_script)

    def test_container_hardening_renders_without_raw_podman_arguments(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            service = self.write(
                directory,
                "service.toml",
                service_toml(
                    container='''network = "host"
container-user = 1000
no-new-privileges = true
drop-capabilities = ["NET_RAW"]
shm-size-mib = 128

[[container.endpoints]]
name = "http"
port = 8080''',
                    krun=(
                        "enabled = true\ncpus = 1\nram-mib = 128\n"
                        'network = "tap"\n'
                        'ipv4 = "10.253.99.2/30"\n'
                        'probe-endpoint = "http"'
                    ),
                ),
            )
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(Fleet.build([service]))
            }
        unit = artifacts[
            Path("etc/containers/systemd/users/51999/service.container")
        ]
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("DropCapability=NET_RAW", unit)
        self.assertIn("ShmSize=128m", unit)
        self.assertIn("User=1000:1000\n", unit)
        self.assertIn("UserNS=keep-id:uid=1000,gid=1000", unit)

    def test_zero_container_user_renders_without_uid_mapping(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            service = self.write(
                directory,
                "service.toml",
                service_toml(
                    container='''network = "host"
container-user = 0

[[container.endpoints]]
name = "http"
port = 8080''',
                    # Keep the fixture valid for Fleet's active-TAP
                    # requirement. The identity assertion is independent of
                    # networking.
                    krun=(
                        "enabled = true\ncpus = 1\nram-mib = 128\n"
                        'network = "tap"\n'
                        'ipv4 = "10.253.99.2/30"\n'
                        'probe-endpoint = "http"'
                    ),
                ),
            )
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(Fleet.build([service]))
            }

        unit = artifacts[
            Path("etc/containers/systemd/users/51999/service.container")
        ]
        self.assertIn("User=0\n", unit)
        self.assertNotIn("UserNS=", unit)


if __name__ == "__main__":
    unittest.main()
