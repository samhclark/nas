# ABOUTME: Checks the source-level VictoriaLogs pilot integration contract.

from __future__ import annotations

import unittest
from pathlib import Path

from quadletgen.parser import load_service


REPO = Path(__file__).resolve().parents[1]
SERVICE_PATH = REPO / "quadlets/victoria-logs.toml"
GRAFANA_PATH = (
    REPO
    / "overlay-root/usr/share/nas/grafana/provisioning/datasources/datasources.yml"
)
PROMSCRAPE_PATH = REPO / "overlay-root/usr/share/nas/victoria-metrics/promscrape.yml"
VM_SMOKE_PATH = REPO / "tests/vm-smoke.bu"
GRAFANA_TOML_PATH = REPO / "quadlets/grafana.toml"
QUADLET_PATH = (
    REPO
    / "overlay-root/etc/containers/systemd/users/51270/victoria-logs.container"
)
MANIFEST_PATH = (
    REPO / "overlay-root/usr/share/nas/storage/victoria-logs.storage-manifest"
)
PREPARE_UNIT_PATH = (
    REPO
    / "overlay-root/etc/systemd/system/nas-prepare-victoria-logs-storage.service"
)
FILTER_PATH = REPO / "overlay-root/etc/nftables/nas-krun-filter.nft"
NAT_PATH = REPO / "overlay-root/etc/nftables/nas-krun-nat.nft"


class VictoriaLogsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = load_service(SERVICE_PATH)
        cls.grafana = GRAFANA_PATH.read_text()
        cls.promscrape = PROMSCRAPE_PATH.read_text()

    def test_service_identity_network_endpoint_and_storage(self):
        service = self.service
        self.assertEqual(service.info.name, "victoria-logs")
        self.assertEqual(service.info.application, "observability")
        self.assertEqual(service.info.role, "logs")
        self.assertEqual(service.host.username, "_nas_victorialogs")
        self.assertEqual(service.host.uid, 51270)
        self.assertEqual(service.host.subid_start, 512700000)
        self.assertEqual(str(service.tap_guest), "10.253.18.2/30")
        endpoint = service.endpoints_by_name["http"]
        self.assertEqual(endpoint.port, 9428)
        self.assertEqual(endpoint.publication, "127.0.0.1:9428")
        self.assertEqual(endpoint.consumers, ("caddy", "grafana", "victoria-metrics"))
        storage = service.storage[0]
        self.assertEqual(storage.dataset, "tank/victoria-logs/data")
        self.assertEqual(storage.host_path, "/var/lib/victoria-logs")
        self.assertEqual(storage.record_size.value, "128K")
        self.assertEqual(storage.compression.value, "lz4")
        self.assertFalse(storage.atime)

    def test_image_and_runtime_limits_are_pinned(self):
        self.assertRegex(
            self.service.container.image,
            r"victoria-logs:v1\.52\.0@sha256:[0-9a-f]{64}$",
        )
        command = self.service.container.exec
        assert command is not None
        for flag in (
            "-retentionPeriod=7d",
            "-retention.maxDiskUsagePercent=80",
            "-search.maxQueryTimeRange=7d",
            "-search.maxQueryDuration=15s",
            "-search.maxConcurrentRequests=2",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, command)

    def test_grafana_provisions_non_default_logs_datasource(self):
        self.assertIn("  - name: VictoriaLogs\n", self.grafana)
        self.assertIn("    type: victoriametrics-logs-datasource\n", self.grafana)
        self.assertIn("    access: proxy\n", self.grafana)
        self.assertIn("    url: http://victoria-logs.krun:9428\n", self.grafana)
        self.assertIn("    isDefault: false\n", self.grafana)
        self.assertIn("      maxLines: 50\n", self.grafana)
        self.assertIn(
            "victoriametrics-logs-datasource@0.31.0",
            GRAFANA_TOML_PATH.read_text(),
        )

    def test_victoria_metrics_scrapes_logs_metrics(self):
        self.assertIn("  - job_name: 'victoria-logs'\n", self.promscrape)
        self.assertIn("      - targets: ['victoria-logs.krun:9428']\n", self.promscrape)

    def test_generated_runtime_enforces_storage_and_network_contracts(self):
        quadlet = QUADLET_PATH.read_text()
        manifest = MANIFEST_PATH.read_text()
        prepare_unit = PREPARE_UNIT_PATH.read_text()
        firewall = FILTER_PATH.read_text()
        nat = NAT_PATH.read_text()

        self.assertIn("Annotation=krun.cpus=1\n", quadlet)
        self.assertIn("Annotation=krun.ram_mib=1024\n", quadlet)
        self.assertIn("Annotation=krun.tap_name=krun-51270\n", quadlet)
        self.assertIn("Volume=/var/lib/victoria-logs:/victoria-logs-data\n", quadlet)
        self.assertIn("/run/nas-storage/victoria-logs/ready", quadlet)
        self.assertIn("--owner 51270:51270 --access rwx", quadlet)
        self.assertIn(
            "managed-zfs|tank/victoria-logs/data|/var/lib/victoria-logs|0750|"
            "recordsize=128K,compression=lz4,atime=off,primarycache=all",
            manifest,
        )
        self.assertIn(
            "Requires=zfs.target ensure-nas-victorialogs-account.service",
            prepare_unit,
        )
        self.assertIn(
            'iifname "krun-51210" oifname "krun-51270" '
            "ip saddr 10.253.3.2 ip daddr 10.253.18.2 tcp dport { 9428 } accept",
            firewall,
        )
        self.assertIn(
            'iifname "krun-51250" oifname "krun-51270" '
            "ip saddr 10.253.7.2 ip daddr 10.253.18.2 tcp dport { 9428 } accept",
            firewall,
        )
        self.assertIn(
            'ip daddr 127.0.0.1 tcp dport 9428 dnat to 10.253.18.2:9428',
            nat,
        )

    def test_generated_identity_and_no_snapshot_policy(self):
        self.assertIn(
            "_nas_victorialogs:512700000:65536",
            (REPO / "overlay-root/etc/subuid").read_text(),
        )
        self.assertIn(
            "_nas_victorialogs:512700000:65536",
            (REPO / "overlay-root/etc/subgid").read_text(),
        )
        snapshot_units = "\n".join(
            path.name
            for path in (REPO / "overlay-root/etc/systemd/system").glob(
                "zfs-snapshots-*.timer"
            )
        )
        self.assertNotIn("victoria-logs", snapshot_units)

    def test_vm_smoke_masks_new_zfs_preparation_unit(self):
        self.assertIn(
            "name: nas-prepare-victoria-logs-storage.service\n      mask: true",
            VM_SMOKE_PATH.read_text(),
        )


if __name__ == "__main__":
    unittest.main()
