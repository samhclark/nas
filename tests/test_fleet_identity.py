# ABOUTME: Tests authored fleet groups and rootless mapped identities.

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from quadletgen.compiler import compile_fleet
from quadletgen.model import ConfigError, Fleet, FleetGroup
from quadletgen.parser import load_fleet, load_service, parse_fleet_groups
from tests.quadlet_test_support import REPO, parse_fleet_fixture, service_toml


class FleetIdentityTests(unittest.TestCase):
    def write_service(self, directory: Path, source: str):
        path = directory / "service.toml"
        path.write_text(source)
        return load_service(path)

    def write_groups(self, directory: Path, source: str):
        path = directory / "_fleet.toml"
        path.write_text(source)
        return parse_fleet_fixture(path, parse_fleet_groups)

    def valid_service(self, *, identity: str = "") -> str:
        return service_toml(
            container=(
                'network = "host"\n\n'
                "[[container.endpoints]]\n"
                'name = "http"\n'
                "port = 8080"
            ),
            krun=(
                "enabled = true\ncpus = 1\nram-mib = 128\n"
                'network = "tap"\n'
                'ipv4 = "10.253.99.2/30"\n'
                'probe-endpoint = "http"'
            ),
            extra=identity,
        )

    def test_fleet_loader_reads_config_once_and_handles_missing_config(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            (directory / "service.toml").write_text(self.valid_service())
            self.assertEqual(load_fleet(directory).groups, ())
            fleet_path = directory / "_fleet.toml"
            fleet_path.write_text('[[groups]]\nname = "media"\ngid = 52000\n')
            original_open = Path.open
            opened = []

            def tracking_open(path, *args, **kwargs):
                opened.append(path)
                return original_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", tracking_open):
                fleet = load_fleet(directory)
            self.assertEqual(opened.count(fleet_path), 1)
            self.assertEqual(fleet.groups[0].name, "media")
            fleet_path.write_text('unknown = true\n')
            with self.assertRaisesRegex(ConfigError, "unknown keys: unknown"):
                load_fleet(directory)

    def test_parser_reads_empty_and_declared_fleet_groups(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            empty = directory / "_fleet-empty.toml"
            empty.write_text("# no groups yet\n")
            self.assertEqual(parse_fleet_fixture(empty, parse_fleet_groups), ())

            groups = self.write_groups(
                directory,
                '[[groups]]\nname = "media"\ngid = 52000\n',
            )
            service = self.write_service(
                directory,
                self.valid_service(
                    identity=(
                        "\n[identity]\n"
                        'supplemental-groups = ["media"]\n'
                        "mapped-container-id = 1000\n"
                        'mapped-group = "media"\n'
                    )
                ).replace(
                    "[container]\n",
                    "[container]\ncontainer-user = 1000\n",
                ),
            )

        self.assertEqual(groups, (FleetGroup("media", 52000),))
        self.assertEqual(service.identity.supplemental_groups, ("media",))
        self.assertEqual(service.identity.mapped_container_id, 1000)
        self.assertEqual(service.identity.mapped_group, "media")

    def test_compiler_renders_mapped_ids_group_membership_and_only_subgid_auth(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            service = self.write_service(
                directory,
                self.valid_service(
                    identity=(
                        "\n[identity]\n"
                        'supplemental-groups = ["media"]\n'
                        "mapped-container-id = 1000\n"
                        'mapped-group = "media"\n'
                    )
                ).replace(
                    "[container]\n",
                    "[container]\ncontainer-user = 1000\n",
                ),
            )
            groups = self.write_groups(
                directory,
                '[[groups]]\nname = "media"\ngid = 52000\n',
            )
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(Fleet.build([service], groups))
            }

        unit = artifacts[
            Path("etc/containers/systemd/users/51999/service.container")
        ]
        self.assertIn("UIDMap=+u1000:@51999:1", unit)
        self.assertIn("GIDMap=+g1000:@52000:1", unit)
        self.assertIn("User=1000:1000\n", unit)
        self.assertIn("GroupAdd=keep-groups", unit)
        self.assertNotIn("UserNS=", unit)

        sysusers = artifacts[Path("usr/lib/sysusers.d/nas-fleet-groups.conf")]
        self.assertIn("g media 52000\n", sysusers)
        self.assertIn("m _nas_service media\n", sysusers)

        self.assertIn("_nas_service:519990000:65536\n", artifacts[Path("etc/subuid")])
        self.assertNotIn("_nas_service:52000:1", artifacts[Path("etc/subuid")])
        self.assertIn("_nas_service:519990000:65536\n", artifacts[Path("etc/subgid")])
        self.assertIn("_nas_service:52000:1\n", artifacts[Path("etc/subgid")])
        account_script = artifacts[
            Path("usr/local/bin/ensure-nas-service-account.sh")
        ]
        self.assertIn(
            'ensure_exact_subid_entry /etc/subgid "52000" "1"',
            account_script,
        )

    def test_rejects_undefined_groups_duplicate_groups_and_conflicts(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            undefined = self.write_service(
                directory,
                self.valid_service(
                    identity=(
                        "\n[identity]\n"
                        'supplemental-groups = ["missing"]\n'
                    )
                ),
            )
            with self.assertRaisesRegex(ConfigError, "undefined fleet group"):
                Fleet.build([undefined])

            duplicate_name = self.write_groups(
                directory,
                (
                    '[[groups]]\nname = "media"\ngid = 52000\n'
                    '[[groups]]\nname = "media"\ngid = 52001\n'
                ),
            )
            with self.assertRaisesRegex(ConfigError, "duplicate group name"):
                Fleet.build([undefined], duplicate_name)

            duplicate_gid = self.write_groups(
                directory,
                (
                    '[[groups]]\nname = "media"\ngid = 52000\n'
                    '[[groups]]\nname = "other"\ngid = 52000\n'
                ),
            )
            with self.assertRaisesRegex(ConfigError, "duplicate group gid"):
                Fleet.build([undefined], duplicate_gid)

    def test_requires_mapped_container_user_for_active_tap(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            for container_user in (None, 999):
                with self.subTest(container_user=container_user):
                    source = self.valid_service(
                        identity="\n[identity]\nmapped-container-id = 1000\n"
                    )
                    if container_user is not None:
                        source = source.replace(
                            "[container]\n",
                            f"[container]\ncontainer-user = {container_user}\n",
                        )
                    with self.assertRaisesRegex(
                        ConfigError,
                        "active TAP.*container-user.*mapped-container-id",
                    ):
                        self.write_service(directory, source)

            valid = self.write_service(
                directory,
                self.valid_service(
                    identity="\n[identity]\nmapped-container-id = 1000\n"
                ).replace(
                    "[container]\n",
                    "[container]\ncontainer-user = 1000\n",
                ),
            )
            self.assertEqual(valid.container.container_user, 1000)

            with self.assertRaisesRegex(ConfigError, "mapped-container-id"):
                self.write_service(
                    directory,
                    self.valid_service(
                        identity="\n[identity]\nmapped-container-id = -1\n"
                    ),
                )

            with self.assertRaisesRegex(ConfigError, "mapped-container-id"):
                self.write_service(
                    directory,
                    self.valid_service(
                        identity="\n[identity]\nmapped-container-id = 0\n"
                    ),
                )

            with self.assertRaisesRegex(ConfigError, r"groups.*gid"):
                self.write_groups(
                    directory,
                    '[[groups]]\nname = "media"\ngid = 0\n',
                )


if __name__ == "__main__":
    unittest.main()
