# ABOUTME: Tests the typed fleet-owned media resource and service exports.
# ABOUTME: Keeps resource validation separate from service-local storage tests.

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quadletgen.compiler import compile_fleet
from quadletgen.model import ConfigError, Fleet
from quadletgen.parser import load_fleet, load_service
from quadletgen.render_storage import shared_storage_manifest, shared_storage_unit
from tests.quadlet_test_support import REPO, service_toml


FLEET = """
[[groups]]
name = "media"
gid = 52000

[[resources]]
name = "media"
kind = "existing-zfs"
dataset = "tank/videos"
host-path = "/var/zfs/tank/videos"
shared-group = "media"
mode = "2775"
required-paths = ["data", "data/media"]
"""


EXPORT = """
[[shared-storage]]
resource = "media"
subpath = "data/media"
container-path = "/data/media"
access = "read-only"
"""


class SharedStorageTests(unittest.TestCase):
    def load(self, *, export: str = EXPORT, identity: str = ""):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            fleet_path = directory / "_fleet.toml"
            service_path = directory / "service.toml"
            fleet_path.write_text(FLEET)
            service_path.write_text(
                service_toml(
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
                    extra=identity + export,
                )
            )
            return load_fleet(directory)

    def test_parser_model_and_rendering_share_one_named_resource(self):
        fleet = self.load()
        resource = fleet.resources_by_name["media"]
        service = fleet.services[0]

        self.assertEqual(resource.required_paths, ("data", "data/media"))
        self.assertEqual(service.shared_storage[0].container_path, "/data/media")
        artifacts = {
            artifact.path: artifact.content for artifact in compile_fleet(fleet)
        }
        container = artifacts[Path("etc/containers/systemd/users/51999/service.container")]
        self.assertIn(
            "Volume=/var/zfs/tank/videos/data/media:/data/media:ro",
            container,
        )
        self.assertIn(
            "marker /run/nas-storage/media/ready 300 2 "
            "--path /var/zfs/tank/videos/data/media --source tank/videos --access rx",
            container,
        )
        self.assertIn("resource|media|media|52000|2775|tank/videos", shared_storage_manifest(resource, 52000))
        self.assertIn("nas-prepare-shared-storage.sh", shared_storage_unit(resource))
        paths = artifacts[
            Path("usr/share/nas/fleet/shared-storage-paths.list")
        ]
        self.assertIn("/var/zfs/tank/videos/data/media\n", paths)

    def test_writable_export_requires_shared_group_membership(self):
        with self.assertRaisesRegex(ConfigError, "read-write access requires fleet group"):
            self.load(
                export=EXPORT.replace("read-only", "read-write")
            )

        fleet = self.load(
            export=EXPORT.replace("read-only", "read-write"),
            identity='[identity]\nsupplemental-groups = ["media"]\n',
        )
        self.assertEqual(fleet.services[0].shared_storage[0].access.value, "read-write")

    def test_unknown_and_undeclared_subpaths_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown fleet resource"):
            self.load(export=EXPORT.replace('resource = "media"', 'resource = "missing"'))
        with self.assertRaisesRegex(ConfigError, "is not declared"):
            self.load(export=EXPORT.replace('subpath = "data/media"', 'subpath = "data/other"'))

    def test_shared_export_cannot_collide_with_raw_or_local_storage_mounts(self):
        raw = (
            '[[container.volumes]]\n'
            'source = "/var/tmp/raw"\n'
            'target = "/data/media"\n'
        )
        with self.assertRaisesRegex(ConfigError, "already declared"):
            self.load(export=EXPORT.replace("[[shared-storage]]", raw + "\n[[shared-storage]]"))


if __name__ == "__main__":
    unittest.main()
