# ABOUTME: Locks the native Vector host collector, journal policy, and image import.

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CONTAINERFILE = (REPO / "Containerfile").read_text()
JOURNALD = (
    REPO / "overlay-root/etc/systemd/journald.conf.d/10-nas-persistent.conf"
).read_text()
SERVICE = (
    REPO / "overlay-root/etc/systemd/system/nas-vector.service"
).read_text()
CONFIG = (REPO / "overlay-root/etc/vector/vector.yaml").read_text()
IMAGE_CONTRACT = (REPO / "tests/image-contract.sh").read_text()
VM_SMOKE = (REPO / "tests/vm-smoke.bu").read_text()


class VectorHostTests(unittest.TestCase):
    def test_journald_is_persistent_and_bounded_without_reducing_runtime_diagnostics(self):
        for setting in (
            "Storage=persistent",
            "Compress=yes",
            "SystemMaxUse=512M",
            "SystemKeepFree=2G",
            "SystemMaxFileSize=64M",
            "MaxRetentionSec=7day",
            "RuntimeMaxUse=128M",
            "RuntimeKeepFree=256M",
            "RuntimeMaxFileSize=32M",
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, JOURNALD)
        self.assertNotIn("MaxLevelStore", JOURNALD)

    def test_vector_reads_selected_uids_and_buffers_on_ssd_state(self):
        self.assertIn("data_dir: /var/lib/nas-vector", CONFIG)
        self.assertIn("type: journald", CONFIG)
        self.assertIn('_UID:\n        - "51310"', CONFIG)
        self.assertIn('_UID:\n        - "51250"', CONFIG)
        self.assertIn("current_boot_only: false", CONFIG)
        self.assertIn("since_now: false", CONFIG)
        self.assertIn("uri: http://127.0.0.1:9428/insert/jsonline?", CONFIG)
        self.assertIn("_stream_fields=host,service", CONFIG)
        self.assertIn("_msg_field=message", CONFIG)
        self.assertIn("_time_field=timestamp", CONFIG)
        self.assertIn("compression: gzip", CONFIG)
        self.assertIn("method: newline_delimited", CONFIG)
        self.assertIn("enabled: false", CONFIG)
        self.assertIn("type: disk", CONFIG)
        self.assertIn("max_size: 1073741824", CONFIG)
        self.assertIn("when_full: block", CONFIG)
        self.assertNotIn("/run/nas-vector", CONFIG)

    def test_vector_service_is_independent_of_backend_and_hardened(self):
        self.assertIn("After=local-fs.target systemd-journal-flush.service", SERVICE)
        self.assertNotIn("network-online.target", SERVICE)
        self.assertNotIn("victoria-logs", SERVICE)
        self.assertIn("ExecStart=/usr/local/bin/vector --config /etc/vector/vector.yaml", SERVICE)
        for directive in (
            "DynamicUser=yes",
            "SupplementaryGroups=systemd-journal",
            "StateDirectory=nas-vector",
            "RuntimeDirectory=nas-vector",
            "NoNewPrivileges=yes",
            "CapabilityBoundingSet=",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, SERVICE)

    def test_vector_image_is_digest_pinned_and_validated(self):
        versions = {}
        for stage, variant in (
            ("vector", "distroless-static"),
            ("vector-license", "debian"),
        ):
            with self.subTest(stage=stage):
                match = re.search(
                    r"^FROM docker\.io/timberio/vector:"
                    rf"([0-9]+\.[0-9]+\.[0-9]+)-{variant}"
                    rf"@sha256:[0-9a-f]{{64}} AS {stage}$",
                    CONTAINERFILE,
                    re.MULTILINE,
                )
                self.assertIsNotNone(match)
                assert match is not None
                versions[stage] = match[1]
        self.assertEqual(versions["vector"], versions["vector-license"])
        self.assertIn(
            "COPY --from=vector /usr/local/bin/vector /usr/local/bin/vector",
            CONTAINERFILE,
        )
        self.assertIn(
            "COPY --from=vector-license /usr/share/vector/NOTICE",
            CONTAINERFILE,
        )
        self.assertIn(
            f'/usr/local/bin/vector --version | grep -F "vector {versions["vector"]}"',
            CONTAINERFILE,
        )
        self.assertIn(
            f"/usr/local/bin/vector --version | grep -Fq 'vector {versions['vector']}'",
            IMAGE_CONTRACT,
        )
        self.assertIn("--config-yaml /etc/vector/vector.yaml", CONTAINERFILE)
        self.assertIn("--no-environment --skip-healthchecks", CONTAINERFILE)

    def test_image_and_vm_contracts_include_vector_lifecycle(self):
        self.assertIn("nas-vector.service", IMAGE_CONTRACT)
        self.assertIn("- name: nas-vector.service\n      mask: true", VM_SMOKE)


if __name__ == "__main__":
    unittest.main()
