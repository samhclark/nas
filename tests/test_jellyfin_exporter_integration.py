# ABOUTME: Validates the generated-service contract for Jellyfin playback
# telemetry, including isolation, secrets, and loopback-only metrics.

import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TOML_PATH = REPO / "quadlets/jellyfin-exporter.toml"
CONTAINERFILE = (REPO / "Containerfile").read_text()
ASSET_MANIFEST = (
    REPO / "overlay-root/usr/share/nas/fleet/assets.list"
).read_text()
EXPORTER = (
    REPO / "overlay-root/usr/share/nas/jellyfin-exporter/jellyfin_exporter.py"
).read_text()


class JellyfinExporterIntegrationTests(unittest.TestCase):
    def test_service_is_small_rootless_krun_workload(self):
        with TOML_PATH.open("rb") as stream:
            config = tomllib.load(stream)

        self.assertEqual(config["host"]["username"], "_nas_jellyfinmetrics")
        self.assertEqual(config["host"]["uid"], 51260)
        self.assertEqual(config["krun"]["cpus"], 1)
        self.assertEqual(config["krun"]["ram-mib"], 128)
        self.assertEqual(config["krun"]["ipv4"], "10.253.8.2/30")
        self.assertEqual(config["krun"]["probe-endpoint"], "metrics")
        self.assertEqual(config["container"]["secrets"], [{"name": "jellyfin-api-key"}])

    def test_exporter_is_loopback_only_and_reads_key_from_file(self):
        self.assertIn('LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")', EXPORTER)
        self.assertIn('JELLYFIN_API_KEY_FILE", "/run/secrets/jellyfin-api-key"', EXPORTER)

    def test_image_labels_exporter_assets_for_container_access(self):
        self.assertIn(
            'semanage fcontext -a -t container_file_t -r s0 "${asset}(/.*)?"',
            CONTAINERFILE,
        )
        self.assertIn(
            "/usr/share/nas/jellyfin-exporter",
            ASSET_MANIFEST,
        )

    def test_exporter_does_not_emit_remote_address(self):
        self.assertNotIn('session.get("RemoteEndPoint")', EXPORTER)


if __name__ == "__main__":
    unittest.main()
