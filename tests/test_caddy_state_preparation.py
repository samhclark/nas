# ABOUTME: Regression tests for Caddy's declarative storage and rootless shape.

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SERVICE = (
    REPO / "overlay-root/etc/systemd/system/nas-prepare-caddy-storage.service"
).read_text()
MANIFEST = (
    REPO / "overlay-root/usr/share/nas/storage/caddy.storage-manifest"
).read_text()
QUADLET = (
    REPO / "overlay-root/etc/containers/systemd/users/51310/caddy.container"
).read_text()
CADDYFILE = (
    REPO / "overlay-root/usr/share/nas/caddy/Caddyfile"
).read_text()


class CaddyStatePreparationTests(unittest.TestCase):
    def test_manifest_owns_both_persistent_state_trees(self):
        self.assertIn(
            "service|caddy|_nas_caddy|51310|51310|513100000|65536|"
            "80,443,2019|-",
            MANIFEST,
        )
        self.assertIn("directory|/var/lib/caddy|0750", MANIFEST)
        self.assertIn("directory|/var/lib/caddy-config|0750", MANIFEST)
        self.assertNotIn("managed-zfs", MANIFEST)

    def test_generated_service_uses_the_common_storage_runtime(self):
        self.assertIn("Requires=ensure-nas-caddy-account.service", SERVICE)
        self.assertNotIn("zfs.target", SERVICE)
        self.assertIn(
            "ExecStart=/usr/local/bin/nas-prepare-storage.sh "
            "/usr/share/nas/storage/caddy.storage-manifest",
            SERVICE,
        )
        self.assertIn("TimeoutStartSec=infinity", SERVICE)

    def test_quadlet_waits_for_readiness_and_keeps_large_state_unlabeled(self):
        self.assertIn(
            "nas-wait-for-readiness.sh marker "
            "/run/nas-storage/caddy/ready 300 2",
            QUADLET,
        )
        self.assertNotIn("/usr/bin/test -w /var/lib/caddy", QUADLET)
        self.assertNotIn("/usr/bin/test -w /var/lib/caddy-config", QUADLET)
        self.assertIn("Volume=/var/lib/caddy:/data\n", QUADLET)
        self.assertIn("Volume=/var/lib/caddy-config:/config\n", QUADLET)
        self.assertNotIn("Volume=/var/lib/caddy:/data:Z", QUADLET)
        self.assertNotIn("Volume=/var/lib/caddy-config:/config:Z", QUADLET)
        self.assertNotIn("nas-migrations", QUADLET)
        self.assertNotIn("prepare-caddy-state", QUADLET)

    def test_runtime_secret_mount_is_unchanged(self):
        self.assertIn(
            "Volume=/run/nas-secrets/caddy/cf-api-token:"
            "/run/secrets/cf-api-token:ro,Z",
            QUADLET,
        )

    def test_quadlet_uses_root_managed_tap_and_no_podman_forwarders(self):
        self.assertIn("PodmanArgs=--runtime=krun", QUADLET)
        self.assertIn("Annotation=krun.cpus=2", QUADLET)
        self.assertIn("Annotation=krun.ram_mib=512", QUADLET)
        self.assertIn("Annotation=krun.tap_name=krun-51310", QUADLET)
        self.assertIn("StopSignal=SIGINT", QUADLET)
        self.assertNotIn("Sysctl=net.ipv4.ip_unprivileged_port_start", QUADLET)
        self.assertIn("DNS=100.100.100.100", QUADLET)
        self.assertNotIn("DNS=75.75.75.75", QUADLET)
        self.assertNotIn("DNS=75.75.76.76", QUADLET)
        self.assertNotIn("DNS=127.", QUADLET)

    def test_caddyfile_uses_tap_service_names_and_allows_http3(self):
        self.assertIn("admin 0.0.0.0:2019", CADDYFILE)
        for target in (
            "grafana.krun:3000",
            "garage.krun:3900",
            "garage.krun:3903",
            "jellyfin.krun:8096",
            "victoria-metrics.krun:8428",
        ):
            self.assertIn(f"reverse_proxy {target}", CADDYFILE)
        self.assertNotIn("protocols h1 h2", CADDYFILE)


if __name__ == "__main__":
    unittest.main()
