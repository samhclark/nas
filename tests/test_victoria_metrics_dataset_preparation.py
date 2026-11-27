# ABOUTME: Regression tests for VictoriaMetrics steady-state ZFS preparation.

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SERVICE = (
    REPO
    / "overlay-root/etc/systemd/system/nas-prepare-victoria-metrics-storage.service"
).read_text()
MANIFEST = (
    REPO
    / "overlay-root/usr/share/nas/storage/"
    "victoria-metrics.storage-manifest"
).read_text()
QUADLET = (
    REPO
    / "overlay-root/etc/containers/systemd/users/51250/victoria-metrics.container"
).read_text()


class VictoriaMetricsDatasetPreparationTests(unittest.TestCase):
    def test_preparation_waits_for_service_identity(self):
        self.assertIn(
            "Requires=zfs.target ensure-nas-victoriametrics-account.service",
            SERVICE,
        )
        self.assertIn(
            "After=zfs.target local-fs.target "
            "ensure-nas-victoriametrics-account.service",
            SERVICE,
        )

    def test_manifest_declares_creation_only_dataset_policy(self):
        self.assertIn(
            "managed-zfs|tank/victoria-metrics|none|-|-",
            MANIFEST,
        )
        self.assertIn(
            "managed-zfs|tank/victoria-metrics/data|"
            "/var/lib/victoria-metrics|0750|"
            "recordsize=128K,compression=lz4,atime=off,primarycache=all",
            MANIFEST,
        )

    def test_quadlet_uses_derived_mount_and_readiness(self):
        self.assertIn(
            "Volume=/var/lib/victoria-metrics:/victoria-metrics-data",
            QUADLET,
        )
        self.assertIn("/run/nas-storage/victoria-metrics/ready", QUADLET)
        self.assertIn(
            "--source tank/victoria-metrics/data --owner 51250:51250",
            QUADLET,
        )


if __name__ == "__main__":
    unittest.main()
