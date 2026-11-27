# ABOUTME: Regression tests for Garage's declarative multi-dataset storage.

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
QUADLET = (
    REPO / "overlay-root/etc/containers/systemd/users/51110/garage.container"
).read_text()
SERVICE = (
    REPO / "overlay-root/etc/systemd/system/nas-prepare-garage-storage.service"
).read_text()
MANIFEST = (
    REPO / "overlay-root/usr/share/nas/storage/garage.storage-manifest"
).read_text()


class GarageDatasetPreparationTests(unittest.TestCase):
    def test_manifest_groups_parent_metadata_and_data(self):
        self.assertIn(
            "managed-zfs|tank/garage|none|-|-",
            MANIFEST,
        )
        self.assertIn(
            "managed-zfs|tank/garage/meta|/var/lib/garage/meta|0750|"
            "recordsize=4K,compression=lz4,atime=off,primarycache=metadata",
            MANIFEST,
        )
        self.assertIn(
            "managed-zfs|tank/garage/data|/var/lib/garage/data|0750|"
            "recordsize=1M,compression=off,atime=off,primarycache=all",
            MANIFEST,
        )

    def test_one_service_prepares_the_atomic_storage_group(self):
        self.assertIn("Requires=zfs.target ensure-nas-garage-account.service", SERVICE)
        self.assertIn(
            "/usr/share/nas/storage/garage.storage-manifest",
            SERVICE,
        )
        self.assertEqual(MANIFEST.count("service|garage|"), 1)

    def test_quadlet_waits_for_storage_readiness(self):
        self.assertIn("/run/nas-storage/garage/ready", QUADLET)


if __name__ == "__main__":
    unittest.main()
